/* Workspace-scoped diagrams share research-note persistence and image storage. */
(() => {
  const $ = id => document.getElementById(id);
  const workspace = new URLSearchParams(location.search).get('workspace') || 'default';
  const scoped = path => path + (path.includes('?') ? '&' : '?') + 'workspace=' + encodeURIComponent(workspace);
  let projects = [], current = null, selected = -1, dirty = false, drawing = false, start = null, busy = false;
  $('back').href = '/#ws=' + encodeURIComponent(workspace);
  function status(text, error = false) { $('status').textContent = text; $('status').classList.toggle('error', error); }
  async function api(path, options = {}) {
    const response = await fetch(scoped(path), options);
    if (!response.ok) { let data; try { data = await response.json(); } catch (_) {} throw new Error(typeof data?.detail === 'string' ? data.detail : `Request failed (${response.status}). Your unsaved changes are still here.`); }
    return response.json();
  }
  async function run(fn) {
    if (busy) return;
    busy = true;
    document.querySelectorAll('button,input,select,textarea').forEach(el => el.disabled = true);
    try { await fn(); } catch (e) { status(e.message, true); }
    finally { busy = false; document.querySelectorAll('button,input,select,textarea').forEach(el => el.disabled = false); }
  }
  function changed() { dirty = true; status('Unsaved changes — click Save changes to keep your writing.'); }
  function options() {
    $('projects').replaceChildren(new Option('Choose a diagram', ''));
    projects.forEach(p => $('projects').add(new Option(p.title, p.id)));
    $('projects').value = current?.id || '';
  }
  function imageURL(name) { return scoped('/api/research-notes/images/' + name); }
  function paint() {
    $('regions').replaceChildren(); $('sections').replaceChildren();
    current.project.sections.forEach((s, i) => {
      const region = document.createElement('button');
      region.className = 'region' + (i === selected ? ' selected' : '') + (s.body ? ' has-notes' : '');
      region.style.cssText = `left:${s.x*100}%;top:${s.y*100}%;width:${s.width*100}%;height:${s.height*100}%`;
      region.style.zIndex = String(10000 - Math.round(s.width * s.height * 9999));
      region.title = s.title; region.setAttribute('aria-label', s.title); region.onclick = () => edit(i);
      $('regions').append(region);
      const button = document.createElement('button'); button.textContent = s.title; button.onclick = () => edit(i);
      button.setAttribute('aria-pressed', String(i === selected)); $('sections').append(button);
    });
  }
  function preview() { $('preview').replaceChildren(NoteWriting.render(current.project.sections[selected].body, workspace)); }
  function edit(index) {
    if (busy || drawing) return;
    selected = index; $('fields').hidden = index < 0; paint();
    if (index < 0) return;
    const s = current.project.sections[index]; $('section-title').value = s.title; $('notes').value = s.body;
    $('bounds').replaceChildren();
    for (const [key, title] of [['x','Left'],['y','Top'],['width','Width'],['height','Height']]) {
      const label = document.createElement('label'); label.textContent = title + ' (%)';
      const input = document.createElement('input'); input.type = 'number'; input.min = '0'; input.max = '100'; input.step = '.1'; input.value = (s[key]*100).toFixed(1);
      input.onchange = () => { const s = current.project.sections[index]; const value = Number(input.value)/100; const next = {...s, [key]:value};
        if (!Number.isFinite(value) || value < 0 || next.width <= 0 || next.height <= 0 || next.x+next.width > 1.000001 || next.y+next.height > 1.000001) { input.value = (s[key]*100).toFixed(1); status('Keep the section inside the image.', true); return; }
        s[key] = value; changed(); paint(); };
      label.append(input); $('bounds').append(label);
    }
    preview();
  }
  function open(project) {
    current = project ? structuredClone(project) : null; dirty = false; selected = -1;
    $('content').hidden = !current;
    if (current) { $('title').value = current.title; $('diagram').src = imageURL(current.project.image); $('fields').hidden = true; paint(); }
    drawing = false; start = null; $('canvas').classList.remove('drawing'); $('draw').textContent = 'Draw a section';
    options();
  }
  async function upload(file) {
    const data = new FormData(); data.append('file', file);
    return (await api('/api/research-notes/images', {method:'POST', body:data})).url;
  }
  function json(method, body) { return {method, headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)}; }
  $('projects').onchange = () => {
    if (dirty && !confirm('Discard unsaved changes to this diagram?')) { options(); return; }
    open(projects.find(p => p.id === $('projects').value)); status('Click a section to write notes.');
  };
  $('upload').onchange = () => {
    const file = $('upload').files[0]; if (!file) return;
    if (dirty && !confirm('Discard unsaved changes to this diagram?')) return;
    run(async () => {
      status('Uploading diagram…');
      const url = await upload(file);
      const project = await api('/api/research-notes', json('POST', {title:file.name.replace(/\.[^.]+$/, ''), body:'', project:{image:new URL(url, location.href).pathname.split('/').pop(), sections:[]}}));
      projects.push(project); open(project); status('Diagram saved. Detect sections with AI, or draw them yourself.');
    }); $('upload').value = '';
  };
  $('title').oninput = () => { current.title = $('title').value; changed(); };
  $('section-title').oninput = () => { current.project.sections[selected].title = $('section-title').value; changed(); paint(); };
  $('notes').oninput = () => { current.project.sections[selected].body = $('notes').value; changed(); preview(); paint(); };
  $('save').onclick = () => run(async () => {
    if (!current.title.trim() || current.project.sections.some(s => !s.title.trim())) throw new Error('Give the project and every section a title.');
    const saved = await api('/api/research-notes/' + current.id, json('PUT', {title:current.title, body:current.body, project:current.project, revision:current.revision}));
    current = saved; projects = projects.map(p => p.id === saved.id ? saved : p); dirty = false; options(); status('All changes saved.');
  });
  $('detect').onclick = () => run(async () => {
    if (current.project.sections.length && !confirm('Replace sections with new AI suggestions? Existing section notes will be removed only when you save.')) return;
    status('Reading the diagram and locating sections…');
    const result = await api('/api/project-overview/detect', json('POST', {image:current.project.image}));
    current.project.sections = result.sections; selected = -1; $('fields').hidden = true; paint(); changed();
    status(`${result.sections.length} sections suggested. Review their boundaries and save, or draw any missing sections.`);
  });
  $('draw').onclick = () => { drawing = !drawing; $('canvas').classList.toggle('drawing', drawing); $('draw').textContent = drawing ? 'Cancel drawing' : 'Draw a section'; };
  function point(event) { const r = $('canvas').getBoundingClientRect(); return {x:Math.max(0, Math.min(1,(event.clientX-r.left)/r.width)), y:Math.max(0,Math.min(1,(event.clientY-r.top)/r.height))}; }
  $('canvas').onpointerdown = e => { if (!drawing || busy) return; start = point(e); $('canvas').setPointerCapture(e.pointerId); };
  $('canvas').onpointerup = e => {
    if (!start) return; const end = point(e), x = Math.min(start.x,end.x), y = Math.min(start.y,end.y), width = Math.abs(end.x-start.x), height = Math.abs(end.y-start.y); start = null;
    if (width < .005 || height < .005) return;
    if (current.project.sections.length >= 100) { status('A diagram can have up to 100 sections.', true); return; }
    current.project.sections.push({title:'New section',x,y,width,height,body:''}); $('draw').click(); edit(current.project.sections.length-1); changed(); $('section-title').focus(); $('section-title').select();
  };
  $('canvas').onpointercancel = () => { start = null; };
  $('remove').onclick = () => { if (!confirm('Remove this section and its notes?')) return; current.project.sections.splice(selected,1); edit(-1); changed(); };
  $('picture').onchange = () => { const file = $('picture').files[0]; if (!file || selected < 0) return; run(async () => { const url = await upload(file); const s = current.project.sections[selected]; s.body += `\n\n![Picture](${url})\n`; $('notes').value = s.body; changed(); preview(); paint(); }); $('picture').value = ''; };
  window.addEventListener('beforeunload', e => { if (dirty || busy) { e.preventDefault(); e.returnValue = ''; } });
  run(async () => { const [data,state] = await Promise.all([api('/api/research-notes'), api('/api/state')]); projects = data.notes.filter(n => n.project); $('workspace').textContent = state.workspace.name; options(); if (projects.length) open(projects[0]); });
})();
