"""Files survive conversation saves and reach each model as content, not base64 prompt text."""
import base64
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError

from doxograph import agent, chat, chat_api, chats, config, server, store
from doxograph.chat_attachments import Attachment, conversation_blocks
from doxograph.model_errors import ModelInputError
from pdfs import minimal_pdf
from test_agent import fake_claude, fake_codex, stream
from test_providers import select, transport


def attachment(name='results.csv', raw=b'condition,accuracy\nbaseline,0.7', media='text/plain'):
    return {'name': name, 'media_type': media, 'data': base64.b64encode(raw).decode()}


def image_attachment():
    out = io.BytesIO()
    Image.new('RGB', (16, 12), '#2f5d8a').save(out, format='PNG')
    return attachment('chart.png', out.getvalue(), 'image/png')


@pytest.mark.parametrize('file', [attachment(), image_attachment(), attachment('study.pdf', minimal_pdf('A finding.'), 'application/pdf')])
def test_attachment_only_messages_save_reopen_and_stay_in_workspace(file):
    messages = [{'role': 'user', 'content': '', 'attachments': [file]}, {'role': 'assistant', 'content': 'I read the attachment.'}]
    body = {'agent': 'codex', 'messages': messages, 'revision': 0, 'operation': 'b' * 32}
    with TestClient(server.app, base_url='http://127.0.0.1:8765') as client:
        saved = client.put('/api/chats/' + 'a' * 32, json=body)
        assert saved.status_code == 200, saved.text
        assert saved.json()['title'] == file['name']
        reloaded = client.get('/api/chats').json()['chats'][0]
        assert reloaded['messages'][0]['attachments'] == [file]
        other = config.create_workspace('Other')
        assert client.get('/api/chats', headers={'X-Doxograph-Workspace': other['id']}).json()['chats'] == []
        assert chat_api.ChatBody(messages=messages + [{'role': 'user', 'content': 'Explain more.'}])
        assert chats.SavedChat.model_validate(reloaded)


@pytest.mark.parametrize('file', [
    attachment('../secret.txt'), attachment(raw=b'\x00binary'), attachment(raw=b'\xff'),
    attachment(raw=b''), attachment() | {'data': 'not base64'},
    image_attachment() | {'media_type': 'image/jpeg'},
    attachment('image.png', b'fake image', 'image/png'),
    attachment('fake.pdf', b'fake PDF', 'application/pdf'),
    attachment('script.svg', b'<svg/>', 'image/svg+xml'),
    attachment(raw=b'a' * (4 * 1024 * 1024 + 1)),
])
def test_rejects_invalid_unsafe_or_oversized_files(file):
    with pytest.raises(ValidationError):
        Attachment.model_validate(file)


def test_message_and_conversation_limits_apply_on_send_and_save():
    file = attachment()
    for messages in ([{'role': 'user', 'content': '', 'attachments': [file] * 6}],
                     [{'role': 'user', 'content': ''}],
                     [{'role': 'user', 'content': 'Question'}, {'role': 'assistant', 'content': 'Answer', 'attachments': [file]}, {'role': 'user', 'content': 'Again'}],
                     [{'role': 'user', 'attachments': [attachment(raw=b'a' * (4 * 1024 * 1024)), attachment(raw=b'b' * (3 * 1024 * 1024))]}]):
        with pytest.raises(ValidationError):
            chat_api.ChatBody(messages=messages)
        with pytest.raises(ValidationError):
            chats.Conversation(agent='codex', messages=[*messages, {'role': 'assistant', 'content': 'Answer'}])


def test_labels_files_by_turn_and_extracts_pdf_pages_without_base64_text():
    files = [attachment(), image_attachment(), attachment('study.pdf', minimal_pdf(['First page.', 'Second page.']), 'application/pdf')]
    messages = [{'role': 'user', 'content': 'Compare these.', 'attachments': files},
                {'role': 'assistant', 'content': 'They agree.'}, {'role': 'user', 'content': 'Explain the chart.'}]
    blocks = conversation_blocks(messages, text_pdfs=True)
    text = '\n'.join(b.get('text', '') for b in blocks)
    assert 'Message 3 (user)' in text and 'Attachment to message 1: chart.png' in text
    assert 'condition,accuracy' in text and '[Page 2]\nSecond page.' in text
    assert all(file['data'] not in text for file in files)
    assert any(b['type'] == 'image' and b['source']['data'] == files[1]['data'] for b in blocks)
    direct = conversation_blocks(messages)
    assert any(b['type'] == 'document' and b['source']['data'] == files[2]['data'] for b in direct)


def test_scanned_pdf_is_not_silently_omitted_for_text_providers():
    from pypdf import PdfWriter
    out = io.BytesIO()
    pdf = PdfWriter()
    pdf.add_blank_page(width=100, height=100)
    pdf.write(out)
    file = attachment('scan.pdf', out.getvalue(), 'application/pdf')
    Attachment.model_validate(file)
    with pytest.raises(ModelInputError, match='OCR'):
        conversation_blocks([{'role': 'user', 'content': 'Read this', 'attachments': [file]}], text_pdfs=True)


def test_claude_library_receives_real_image_and_document_blocks(monkeypatch):
    config.set_ai_provider('claude_code')
    seen = fake_claude(monkeypatch, stream({'type': 'result', 'result': 'Read the chart.'}))
    files = [image_attachment(), attachment('study.pdf', minimal_pdf('The result.'), 'application/pdf')]
    agent.answer([{'role': 'user', 'content': 'Compare', 'attachments': files}], lambda: 'http://127.0.0.1:8765')
    event = json.loads(seen['prompt'])
    assert '--input-format' in seen['args']
    assert [b['type'] for b in event['message']['content']] == ['text', 'text', 'image', 'text', 'document']
    assert event['message']['content'][2]['source']['data'] == files[0]['data']


def test_codex_passes_temporary_image_files_and_cleans_them_up(monkeypatch):
    from doxograph import codex_cli
    config.set_ai_provider('codex')
    fake_codex(monkeypatch, stream({'type': 'turn.completed'}))
    run = codex_cli._run
    paths = []

    def inspect(args, **kwargs):
        if '--image' in args:
            assert args.index('-') < args.index('--image')
            path = Path(args[args.index('--image') + 1])
            paths.append(path)
            with Image.open(path) as image:
                assert image.size == (16, 12)
            assert 'condition,accuracy' in kwargs['input']
            assert image_attachment()['data'] not in kwargs['input']
        return run(args, **kwargs)

    monkeypatch.setattr(codex_cli, '_run', inspect)
    agent.answer([{'role': 'user', 'content': '', 'attachments': [image_attachment(), attachment()]}], lambda: 'http://127.0.0.1:8765')
    assert len(paths) == 1 and not paths[0].exists()


@pytest.mark.parametrize('mode', ['RGBA', 'P'])
def test_codex_composites_transparent_charts_onto_white(monkeypatch, mode):
    from doxograph import codex_cli
    config.set_ai_provider('codex')
    fake_codex(monkeypatch, stream({'type': 'turn.completed'}))
    run = codex_cli._run
    if mode == 'RGBA':
        chart = Image.new('RGBA', (3, 1), (0, 0, 0, 0))
        chart.putpixel((1, 0), (0, 0, 0, 255))
        chart.putpixel((2, 0), (255, 0, 0, 128))
        expected_color = (255, 127, 127)
    else:
        chart = Image.new('P', (3, 1), 0)
        chart.putpalette([0, 0, 0, 0, 0, 0, 255, 0, 0])
        chart.info['transparency'] = 0
        chart.putpixel((1, 0), 1)
        chart.putpixel((2, 0), 2)
        expected_color = (255, 0, 0)
    raw = io.BytesIO()
    chart.save(raw, format='PNG')
    file = attachment('transparent-chart.png', raw.getvalue(), 'image/png')
    captured = []

    def inspect(args, **kwargs):
        if '--image' in args:
            path = Path(args[args.index('--image') + 1])
            with Image.open(path) as sent:
                assert sent.mode == 'RGB' and sent.size == (3, 1)
                assert sent.getpixel((0, 0)) == (255, 255, 255)
                assert sent.getpixel((1, 0)) == (0, 0, 0)
                assert sent.getpixel((2, 0)) == expected_color
            captured.append(path)
        return run(args, **kwargs)

    monkeypatch.setattr(codex_cli, '_run', inspect)
    codex_cli.Client().create(model='Codex default', system='Read the chart.', messages=[{
        'role': 'user', 'content': conversation_blocks([{'role': 'user', 'content': 'Explain', 'attachments': [file]}])}])
    assert len(captured) == 1 and not captured[0].exists()


@pytest.mark.parametrize('provider', ['ollama', 'openai', 'openai-compatible'])
def test_paper_chat_delivers_images_and_pdf_text_to_api_providers(monkeypatch, provider):
    select(provider)
    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    requests = transport(monkeypatch, {'answer': 'The chart agrees.'})
    store.save_paper(store.new_paper('study'))
    store.pdf_path('study').write_bytes(minimal_pdf('Library paper.'))
    files = [image_attachment(), attachment('extra.pdf', minimal_pdf('Extra finding.'), 'application/pdf')]
    result = chat.answer('study', [{'role': 'user', 'content': 'Compare', 'attachments': files}])
    assert result['answer'] == 'The chart agrees.'
    message = json.loads(requests[0].content)['messages'][1]
    if provider == 'ollama':
        assert message['images'] == [files[0]['data']]
        assert 'Extra finding.' in message['content']
    else:
        assert any(b.get('image_url', {}).get('url') == 'data:image/png;base64,' + files[0]['data'] for b in message['content'])
        assert 'Extra finding.' in json.dumps(message['content'])


def test_paper_claude_receives_attachment_blocks_without_private_notes(monkeypatch):
    from doxograph import extract
    store.save_paper(store.new_paper('study', notes='private notes must stay private'))
    monkeypatch.setattr(extract, 'client', lambda: SimpleNamespace())
    monkeypatch.setattr(extract, 'paper_document', lambda *a: {'type': 'document', 'source': {'type': 'file', 'file_id': 'paper'}})
    seen = []

    def create(api, **kwargs):
        from doxograph.analysis_types import ModelResponse
        seen.extend(kwargs['messages'][0]['content'])
        return ModelResponse.from_text('{"answer":"Read it."}')

    monkeypatch.setattr(extract, '_create', create)
    chat.answer('study', [{'role': 'user', 'content': 'Compare', 'attachments': [image_attachment()]}])
    assert any(b['type'] == 'image' for b in seen)
    assert 'private notes must stay private' not in json.dumps(seen)
