/* Shared offline Markdown, equations, and local images for both writing spaces. */
const NoteWriting = (() => {
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
  const mathHTML = token => {
    try { return katex.renderToString(token.text, {displayMode:token.display, throwOnError:false, trust:false, maxSize:20, maxExpand:1000}); }
    catch (_) { return `<code>${escape(token.raw)}</code>`; }
  };
  const markdown = new marked.Marked({gfm:true, renderer:{html:token => escape(token.text)}, extensions:[
    {name:'noteDisplayMath', level:'block', start:src => src.indexOf('$$'),
      tokenizer(src) {
        const match = /^\$\$[ \t]*\n?([\s\S]+?)\$\$(?:[ \t]*(?:\n|$))?/.exec(src);
        if (match) return {type:'noteDisplayMath', raw:match[0], text:match[1], display:true};
      }, renderer:mathHTML},
    {name:'noteInlineMath', level:'inline', start:src => src.indexOf('$'),
      tokenizer(src) {
        const match = /^(\${1,2})(?!\$)((?:\\[\s\S]|[^$\n])+?)\1(?!\$)/.exec(src);
        if (match) return {type:'noteInlineMath', raw:match[0], text:match[2], display:match[1] === '$$'};
      }, renderer:mathHTML}
  ]});
  function render(body, workspace) {
    // Parse and sanitize off-DOM, then restrict images before anything can load.
    const fragment = DOMPurify.sanitize(markdown.parse(body), {RETURN_DOM_FRAGMENT:true, FORBID_TAGS:['style','form','input','button'], FORBID_ATTR:['id','name']});
    fragment.querySelectorAll('img').forEach(img => {
      let local = false;
      try {
        const url = new URL(img.getAttribute('src') || '', location.href);
        local = url.origin === location.origin && /^\/api\/research-notes\/images\/[a-f0-9]{64}\.(png|jpg|gif|webp)$/.test(url.pathname)
          && url.searchParams.get('workspace') === workspace;
      } catch (_) { /* Malformed image URLs become placeholders too. */ }
      if (!local) {
        const placeholder = document.createElement('span');
        placeholder.className = 'hint';
        placeholder.textContent = `[Image: ${img.alt || 'upload this image to view it offline'}]`;
        img.replaceWith(placeholder);
      } else { img.loading = 'lazy'; img.alt ||= 'Research note image'; }
    });
    fragment.querySelectorAll('a').forEach(link => {
      link.target = '_blank'; link.rel = 'noopener noreferrer';
    });
    return fragment;
  }
  const formats = {
    heading:['\n## ', '', 'Heading'],
    bold:['**', '**', 'text'],
    equation:['\n\n$$\n', '\n$$\n\n', 'E = mc^2'],
    table:['\n\n| Condition | Result |\n| --- | --- |\n| Baseline |  |\n| Intervention |  |\n\n'],
    link:['[', '](https://example.com)', 'link text']
  };
  return {markdown, render, formats};
})();
