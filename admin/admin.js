/* steamprofiler.org - triage. Read everything, set a status, write a reply, delete.

   Runs in its own container, separate from the public listener. The browser
   holds nothing but a session cookie: the ADMIN_TOKEN that the api container
   wants stays in the admin process and is added there, on the way out. That is
   the difference from the old page, which asked for the token and then kept it
   in the browser where any script on the origin could read it. */

const STATES = ['novo', 'lido', 'aceito', 'fazendo', 'feito', 'recusado'];
const PUBLIC_STATES = ['aceito', 'fazendo', 'feito', 'recusado'];
/* store.MIN_MESSAGE, which is what decides whether an appeal counts as one.
   The api refuses an unban without one either way; this copy exists so the
   page can say so before the click instead of after it. */
const MIN_MESSAGE = 10;
let publicSite = '';
const publicUrl = (path) => `${publicSite}${path}`;

/* The cookie rides along on its own; nothing here has a token to add. */
const auth = () => ({});

/* ── One message ───────────────────────────────────────────────────── */

function itemFor(m, reload) {
  const status = h('select', { cls: 'select', attr: { 'aria-label': 'status' } });
  for (const s of STATES) {
    const opt = h('option', { attr: { value: s }, text: t(`state.${s}`) });
    if (s === m.status) opt.selected = true;
    status.append(opt);
  }

  const reply = h('input', {
    cls: 'input',
    attr: { type: 'text', maxlength: '600', placeholder: t('adm.reply_ph') },
  });
  reply.value = m.reply || '';

  const save = h('button', { cls: 'btn', attr: { type: 'button' }, text: t('adm.save') });
  const drop = h('button', { cls: 'btn btn--danger', attr: { type: 'button' }, text: t('adm.delete') });
  const note = h('span', { cls: 'form-note' });

  save.addEventListener('click', async () => {
    save.disabled = true;
    try {
      await post('/update', { id: m.id, status: status.value, reply: reply.value }, auth());
      note.textContent = t(PUBLIC_STATES.includes(status.value)
        ? 'adm.saved_public' : 'adm.saved_private');
      reload();
    } catch (e) {
      note.textContent = e.message;
    }
    save.disabled = false;
  });

  drop.addEventListener('click', async () => {
    // One click away from destroying someone's report; ask.
    if (!confirm(t('adm.confirm_delete', { id: m.id }))) return;
    try {
      await post('/delete', { id: m.id }, auth());
      reload();
    } catch (e) {
      note.textContent = e.message;
    }
  });

  // No unban here. This card is the feedback inbox, which the api serves with
  // kinds=PUBLIC_KINDS and so never contains an appeal - the button that used
  // to be here could not render, and a second way to open the gate is not
  // something to leave lying around now that opening it has a rule. Blocks
  // view, one card per address, is the only place it lives.
  return h('article', { cls: 'item', data: { status: m.status } },
    h('div', { cls: 'card-head' },
      h('h3', { cls: 'card-title', text: `#${m.id} · ${m.title}` }),
      h('span', { cls: 'tag', data: { kind: m.kind }, text: t(`kind.${m.kind}`) }),
      h('span', { cls: 'tag', data: { status: m.status }, text: t(`state.${m.status}`) }),
      m.votes ? h('span', { cls: 'tag', text: m.votes === 1 ? t('adm.vote_one') : t('adm.votes', { n: m.votes }) }) : null),
    h('p', { cls: 'card-body', text: m.message }),
    h('p', { cls: 'card-meta' },
      txt(stamp(m.created_at)),
      m.context ? txt(`  ·  ${t('adm.from', { path: m.context })}`) : null,
      m.contact ? txt(`  ·  ${t('adm.contact')} `) : null,
      m.contact ? h('span', { cls: 'item-contact', text: m.contact }) : null),
    h('div', { cls: 'item-tools' }, status, reply, save, drop, note));
}

/* ── One address ───────────────────────────────────────────────────── */

/* A block is one address, and everything about it goes on one card: what it
   reached for, how hard, how long it has left, whether it has been let back in
   before, and every appeal it has written. Those used to be scattered across
   three places, so answering an appeal meant reading a message here, a ban
   twenty rows down, and guessing the rest. */

function pathList(ban) {
  const tried = (ban && ban.paths) || [];
  if (tried.length) return tried;
  return ban && ban.path ? [ban.path] : [];
}

function banHead(entry) {
  const ban = entry.ban;
  const tried = pathList(ban);
  return h('div', { cls: 'card-head' },
    h('h3', { cls: 'card-title', text: tried.length ? tried[0] : t('adm.block_gone') }),
    ban && ban.repeat
      ? h('span', { cls: 'tag', data: { repeat: '1' }, text: t('adm.repeat', { n: ban.lifts || 0 }) })
      : null,
    ban && ban.active
      ? h('span', { cls: 'tag', data: { status: 'novo' }, text: t('adm.block_open') })
      : h('span', { cls: 'tag', data: { status: 'recusado' }, text: t('adm.block_over') }),
    entry.appeals.length
      ? h('span', { cls: 'tag', text: t('adm.block_appeals', { n: entry.appeals.length }) })
      : null);
}

/* What it tried. One path is a stray dependency scanner; forty is somebody
   working through a list, and the whole point of this line is that the two do
   not look alike on the page. */
function banPaths(ban) {
  const tried = pathList(ban);
  if (tried.length < 2) return null;
  const box = h('p', { cls: 'paths' });
  for (const p of tried) box.append(h('code', { cls: 'path', text: p }));
  const more = (ban.hits || 0) - tried.length;
  if (more > 0) box.append(h('span', { cls: 'path-more', text: t('adm.paths_more', { n: more }) }));
  return box;
}

function banMeta(ban) {
  if (!ban) return null;
  const bits = [t('adm.block_since', { when: stamp(ban.created_at) })];
  if (ban.active && ban.until_at) bits.push(t('adm.block_until', { when: stamp(ban.until_at) }));
  if (ban.hits) bits.push(t('adm.ban_hits', { n: ban.hits }));
  if (ban.lifts) bits.push(t('adm.block_lifts', { n: ban.lifts }));
  return h('p', { cls: 'card-meta', text: bits.join('  \u00b7  ') });
}

/* One appeal, inside the card of the address that wrote it. */
function appealIn(m, reload) {
  const note = h('span', { cls: 'form-note' });
  const reply = h('input', {
    cls: 'input',
    attr: { type: 'text', maxlength: '600', placeholder: t('adm.reply_ph') },
  });
  reply.value = m.reply || '';

  const seen = h('button', { cls: 'btn btn--quiet', attr: { type: 'button' }, text: t('adm.mark_read') });
  seen.addEventListener('click', async () => {
    seen.disabled = true;
    try {
      await post('/update', { id: m.id, status: 'lido', reply: reply.value }, auth());
      reload();
    } catch (e) {
      note.textContent = e.message;
      seen.disabled = false;
    }
  });

  const drop = h('button', { cls: 'btn btn--danger', attr: { type: 'button' }, text: t('adm.delete') });
  drop.addEventListener('click', async () => {
    if (!confirm(t('adm.confirm_delete', { id: m.id }))) return;
    try {
      await post('/delete', { id: m.id }, auth());
      reload();
    } catch (e) {
      note.textContent = e.message;
    }
  });

  return h('article', { cls: 'appeal', data: { status: m.status } },
    h('p', { cls: 'card-meta' },
      txt(`#${m.id}  \u00b7  ${stamp(m.created_at)}`),
      m.status === 'novo' ? h('b', { cls: 'unread', text: ` ${t('state.novo')}` }) : null,
      m.contact ? txt('  \u00b7  ') : null,
      m.contact ? h('span', { cls: 'item-contact', text: m.contact }) : null),
    h('p', { cls: 'card-body', text: m.message }),
    h('div', { cls: 'item-tools' }, reply, seen, drop, note));
}

/* Whether this address has earned the right to be let out early: it came to
   /appeal and wrote something there. Silence is not a case to answer, so the
   button is not offered for it - and the api would refuse the request anyway,
   which is what makes this a label rather than the lock itself. */
function asked(entry) {
  return entry.appeals.some(
    (m) => (m.message || '').trim().length >= MIN_MESSAGE);
}

function blockFor(entry, reload) {
  const ban = entry.ban;
  const note = h('span', { cls: 'form-note' });
  const tools = [];

  if (ban && ban.active && !asked(entry)) {
    // Said plainly, because the missing button is otherwise indistinguishable
    // from a page that failed to draw one.
    tools.push(h('span', { cls: 'form-note', text: t('adm.block_locked') }));
  }
  if (ban && ban.active && asked(entry)) {
    const free = h('button', { cls: 'btn', attr: { type: 'button' }, text: t('adm.unban') });
    free.addEventListener('click', async () => {
      if (!confirm(t('adm.confirm_unban'))) return;
      free.disabled = true;
      try {
        await post('/unban', { ip_hash: entry.ip_hash }, auth());
        note.textContent = t('adm.unbanned');
        reload();
      } catch (e) {
        note.textContent = e.message;
        free.disabled = false;
      }
    });
    tools.push(free);
  }
  tools.push(note);

  return h('article', { cls: 'item block', data: { repeat: ban && ban.repeat ? '1' : null } },
    banHead(entry),
    banPaths(ban),
    banMeta(ban),
    h('p', { cls: 'card-meta hash', text: entry.ip_hash }),
    entry.appeals.length
      ? h('div', { cls: 'appeals' }, ...entry.appeals.map((m) => appealIn(m, reload)))
      : h('p', { cls: 'form-note', text: t('adm.block_silent') }),
    h('div', { cls: 'item-tools' }, ...tools));
}

/* Everything that was shut out and never said a word, folded into one line.

   These are the majority and there is nothing to do with any of them: no
   appeal to read, and no unban, because the api refuses to lift a ban nobody
   asked to have lifted. Listed one card each they buried the two or three
   entries that actually wanted an answer, which is the whole job of this
   view. So they collapse to a count, and open on request - hidden, not
   deleted, because "what is being shut out right now" is still worth being
   able to look at when a real visitor says they cannot get in. */
function quietBlocks(entries, reload) {
  const list = h('div', { cls: 'appeals' });
  list.hidden = true;
  const toggle = h('button', {
    cls: 'btn btn--quiet', attr: { type: 'button' }, text: t('adm.block_show'),
  });
  toggle.addEventListener('click', () => {
    // Built on the first open and kept afterwards. Sixty cards is real work
    // for the browser, and this is the pile nobody looks at most days.
    if (!list.childElementCount) {
      for (const entry of entries) list.append(blockFor(entry, reload));
    }
    list.hidden = !list.hidden;
    toggle.textContent = t(list.hidden ? 'adm.block_show' : 'adm.block_hide');
  });

  return h('article', { cls: 'item block' },
    h('p', { cls: 'card-meta', text: t('adm.block_quiet', { n: entries.length }) }),
    h('div', { cls: 'item-tools' }, toggle),
    list);
}

/* ── Views ─────────────────────────────────────────────────────────── */

function showView(name) {
  for (const b of document.querySelectorAll('#tabs .tab')) {
    if (b.dataset.view === name) b.dataset.on = '1'; else delete b.dataset.on;
  }
  el('view-mail').hidden = name !== 'mail';
  el('view-block').hidden = name !== 'block';
  el('view-blog').hidden = name !== 'blog';
  el('view-census').hidden = name !== 'census';
  // Fetched on open rather than with the inbox: it is a read nobody needs on
  // every sign-in, and the api flushes the in-memory half to answer it.
  if (name === 'census') loadCensus();
}

for (const b of document.querySelectorAll('#tabs .tab')) {
  b.addEventListener('click', () => showView(b.dataset.view));
}

/* ── The blog ───────────────────────────────────────────────────────
   One author, so there is no permission model here: the whole editor is
   behind the same three locks the rest of this page is, and that is the
   answer until accounts exist.

   A post is several texts, not one. The site speaks several languages and prose
   cannot travel as keys the way every other string does, so each language is
   its own pane and any of them may simply be missing - the public page falls
   back to the original and says which language it is showing. */

const BLOG_LANGS = ['en', 'pt', 'ru', 'zh-cn', 'zh-tw'];
let editing = null;         // the post id being edited, or null for a new one
// One per language, plus the fixed address. Once a field is typed into it is
// never suggested over: what is in it is a decision somebody made.
const slugTouched = {};

/* A title as the readable end of an address, for the languages where that is
   arithmetic rather than a judgement. NFD splits an accented letter into the
   letter and the accent and the next line drops the accents, so "ação" comes
   out "acao" and not "a-o". Cyrillic survives none of it and comes out empty,
   which is the signal to ask the model instead. */
function slugify(title) {
  const flat = (title || '').toLowerCase().normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 80)
    .replace(/-+$/, '');
  // Half a title is not a short address for it, it is one word standing in for
  // a sentence. The id alone says less and claims nothing. Same rule as
  // blog.py, which is the one that decides in the end.
  const kept = flat.replace(/-/g, '').length;
  const whole = (title || '').replace(/[^\p{L}\p{N}]/gu, '').length;
  return kept && kept * 2 >= whole ? flat : '';
}

function pane(lang) {
  const title = h('input', {
    cls: 'input', attr: { type: 'text', maxlength: '140', id: `b-title-${lang}` },
  });
  const slug = h('input', {
    cls: 'input', attr: { type: 'text', maxlength: '80', id: `b-slug-${lang}`,
                          autocomplete: 'off', spellcheck: 'false',
                          placeholder: t('adm.slug_lang_ph') },
  });
  slug.addEventListener('input', () => { slugTouched[lang] = true; });
  const lede = h('input', {
    cls: 'input', attr: { type: 'text', maxlength: '400', id: `b-lede-${lang}` },
  });
  const body = h('textarea', {
    cls: 'textarea textarea--tall', attr: { maxlength: '40000', id: `b-body-${lang}` },
  });
  // Tags are words a reader reads, so they belong beside the text they are
  // read with rather than once for the whole post. Left empty, this language
  // shows the original's, which is what every post written before this had.
  const tags = h('input', {
    cls: 'input', attr: { type: 'text', maxlength: '160', id: `b-tags-${lang}`,
                          placeholder: t('adm.blog_tags_ph') },
  });

  // This language's address is suggested from this language's title, and only
  // while the post has never been saved. Renaming an address that is already
  // out there is a thing to do on purpose, in the field, not a thing that
  // happens because a typo was fixed.
  //
  // A Russian title suggests nothing, because nothing survives being stripped
  // to a-z. That is what the button asks the model for.
  title.addEventListener('input', () => {
    if (editing || slugTouched[lang]) return;
    const guess = slugify(title.value);
    if (guess) slug.value = guess;
  });
  const mark = () => { markLangs(); };
  title.addEventListener('input', mark);
  body.addEventListener('input', mark);

  /* The translator, and the flag that says the reader is owed a warning.
     The checkbox is ticked by the button and never by the code that saves,
     so a text this panel did not machine-translate cannot end up marked as
     if it had been. Untick it after reading the result and the notice goes
     away, which is the only claim on this page only a person can make. */
  const machine = h('input', {
    attr: { type: 'checkbox', id: `b-machine-${lang}` },
  });
  const note = h('span', { cls: 'form-note', attr: { id: `b-tr-note-${lang}` } });
  const go = h('button', {
    cls: 'btn btn--quiet', attr: { type: 'button' }, text: t('adm.tr_do'),
  });

  go.addEventListener('click', async () => {
    const from = el('b-origin').value;
    if (from === lang) {
      note.textContent = t('adm.tr_same');
      return;
    }
    const source = {
      title: el(`b-title-${from}`).value,
      lede: el(`b-lede-${from}`).value,
      body: el(`b-body-${from}`).value,
      tags: el(`b-tags-${from}`).value,
    };
    if (!source.title.trim() || !source.body.trim()) {
      note.textContent = t('adm.tr_empty', { lang: from.toUpperCase() });
      return;
    }
    // Overwriting minutes of somebody's typing on a misclick is the one
    // mistake here that cannot be undone with another click.
    if ((title.value.trim() || body.value.trim())
        && !confirm(t('adm.tr_overwrite', { lang: lang.toUpperCase() }))) return;

    go.disabled = true;
    note.textContent = t('adm.tr_working');
    try {
      const got = await post('/blog/translate', { from, to: lang, ...source }, auth());
      title.value = got.title;
      lede.value = got.lede;
      body.value = got.body;
      // Absent rather than empty when the source had none: an answer that did
      // not come back must not clear a field somebody filled by hand.
      if (got.tags != null) tags.value = got.tags;
      machine.checked = true;
      note.textContent = t('adm.tr_done', { seconds: got.seconds, model: got.model });
      markLangs();
    } catch (e) {
      note.textContent = e.message;
    }
    go.disabled = false;
  });

  return h('div', { cls: 'lang-pane', data: { lang } },
    h('div', { cls: 'tr-bar' }, go,
      h('label', { cls: 'tr-flag', attr: { for: `b-machine-${lang}` } },
        machine, h('span', { text: t('adm.tr_flag') })),
      note),
    h('div', { cls: 'field' },
      h('label', { attr: { for: `b-title-${lang}` }, text: t('adm.blog_title') }), title),
    h('div', { cls: 'field' },
      h('label', { attr: { for: `b-slug-${lang}` }, text: t('adm.slug_lang') }), slug),
    h('div', { cls: 'field' },
      h('label', { attr: { for: `b-lede-${lang}` } },
        h('span', { text: t('adm.blog_lede') }),
        txt(' '), h('em', { text: t('adm.blog_optional') })), lede),
    h('div', { cls: 'field' },
      h('label', { attr: { for: `b-tags-${lang}` } },
        h('span', { text: t('adm.blog_tags') }),
        txt(' '), h('em', { text: t('adm.blog_optional') })), tags),
    h('div', { cls: 'field' },
      h('label', { attr: { for: `b-body-${lang}` }, text: t('adm.blog_body') }), body));
}

/* The addresses, written by the machine at home.

   Only the languages that need it are asked for. An English or Portuguese
   title becomes an address by dropping characters, which this page can do
   itself, instantly and the same way every time; Russian and Chinese become an
   address by being transliterated, which is a judgement, and the model on the
   desk is the one making it. Sending every title would be paying a round trip to
   be told what slugify() already said.

   What comes back lands in the fields, not in the post. Reading it before
   saving is the whole point, the same as with the translator. */
async function writeSlugs() {
  const button = el('b-slugs');
  const note = el('b-slugs-note');
  const titles = {};
  let filled = 0;

  for (const lang of BLOG_LANGS) {
    const title = el(`b-title-${lang}`).value.trim();
    if (!title || slugTouched[lang]) continue;
    const guess = slugify(title);
    if (guess) {
      el(`b-slug-${lang}`).value = guess;
      filled += 1;
    } else {
      titles[lang] = title;
    }
  }

  if (!Object.keys(titles).length) {
    note.textContent = filled ? t('adm.slug_ai_done', { seconds: 0, model: '-', n: filled })
      : t('adm.slug_ai_none');
    return;
  }

  button.disabled = true;
  note.textContent = t('adm.slug_ai_working');
  try {
    const got = await post('/blog/slugs', { titles }, auth());
    for (const [lang, slug] of Object.entries(got.slugs || {})) {
      if (!slug) continue;
      el(`b-slug-${lang}`).value = slug;
      filled += 1;
    }
    const failed = Object.keys(got.failed || {});
    note.textContent = t('adm.slug_ai_done', {
      seconds: got.seconds, model: got.model, n: filled,
    }) + (failed.length
      ? ` ${t('adm.slug_ai_failed', { langs: failed.map((l) => l.toUpperCase()).join(', ') })}`
      : '');
  } catch (e) {
    note.textContent = e.message;
  }
  button.disabled = false;
}

function showLang(lang) {
  for (const b of document.querySelectorAll('#lang-tabs .tab')) {
    if (b.dataset.lang === lang) b.dataset.on = '1'; else delete b.dataset.on;
  }
  for (const p of document.querySelectorAll('.lang-pane')) {
    p.hidden = p.dataset.lang !== lang;
  }
}

/* A dot beside each language tab, so "which of the three is written" is
   readable without opening every translation. */
function markLangs() {
  for (const lang of BLOG_LANGS) {
    const written = el(`b-title-${lang}`).value.trim() && el(`b-body-${lang}`).value.trim();
    el(`mark-${lang}`).textContent = written ? '●' : '';
  }
}

function fillEditor(p) {
  editing = p ? p.id : null;
  // A post being opened for editing has addresses somebody chose; a new one has
  // none, and every field is free to suggest.
  for (const lang of BLOG_LANGS) slugTouched[lang] = Boolean(p);
  // The fixed address is the old kind and only shows when it is one: a post
  // saved without one holds its own id there, which is not something to put in
  // a field that means "an extra name for this post".
  el('b-slug').value = p && p.slug !== p.pid ? p.slug : '';
  el('b-status').value = p ? p.status : 'draft';
  el('b-origin').value = p ? p.origin : 'en';
  for (const lang of BLOG_LANGS) {
    const text = (p && p.texts && p.texts[lang]) || {};
    el(`b-title-${lang}`).value = text.title || '';
    el(`b-lede-${lang}`).value = text.lede || '';
    el(`b-slug-${lang}`).value = text.slug || '';
    el(`b-tags-${lang}`).value = text.tags || '';
    el(`b-body-${lang}`).value = text.body || '';
    el(`b-machine-${lang}`).checked = Boolean(text.machine);
    el(`b-tr-note-${lang}`).textContent = '';
  }
  el('editor-bar').textContent = p ? t('adm.blog_editing') : t('adm.blog_new');
  // The id, which is what the address is made of, rather than the row number,
  // which is not: `#7f3c9a` is a thing to recognise in a URL later.
  el('editor-slug').textContent = p ? `#${p.pid}` : t('adm.blog_unsaved');
  const open = el('b-open');
  // Only a published post has an address worth opening; a draft has none.
  open.hidden = !(p && p.status === 'published');
  if (!open.hidden) open.href = publicUrl(p.url);
  el('b-error').hidden = true;
  el('b-slugs-note').textContent = '';
  el('b-note').textContent = '';
  showLang(p ? p.origin : 'en');
  markLangs();
}

function blogRow(p, reload) {
  const text = p.texts[p.origin] || Object.values(p.texts)[0] || {};
  const written = BLOG_LANGS.filter((l) => p.texts[l]);

  const edit = h('button', { cls: 'btn btn--quiet', attr: { type: 'button' }, text: t('adm.blog_edit') });
  edit.addEventListener('click', () => {
    fillEditor(p);
    el('b-slug').scrollIntoView({ block: 'center', behavior: still() ? 'auto' : 'smooth' });
  });

  const note = h('span', { cls: 'form-note' });
  const drop = h('button', { cls: 'btn btn--danger', attr: { type: 'button' }, text: t('adm.delete') });
  drop.addEventListener('click', async () => {
    if (!confirm(t('adm.blog_confirm_delete', { slug: p.pid }))) return;
    try {
      await post('/blog/delete', { id: p.id }, auth());
      if (editing === p.id) fillEditor(null);
      reload();
    } catch (e) {
      note.textContent = e.message;
    }
  });

  const when = p.published_at || p.updated_at;
  const draft = p.status === 'draft';
  return h('article', { cls: 'item', data: draft ? { status: 'novo' } : {} },
    h('div', { cls: 'card-head' },
      h('h3', { cls: 'card-title', text: text.title || p.slug }),
      h('span', { cls: 'tag', data: { status: draft ? 'novo' : 'feito' },
                  text: t(draft ? 'blog.draft' : 'blog.live') }),
      h('span', { cls: 'tag', text: written.map((l) => l.toUpperCase()).join(' ') }),
      p.votes ? h('span', { cls: 'tag', text: t('adm.votes', { n: p.votes }) }) : null),
    h('p', { cls: 'card-meta' },
      txt(p.url),
      when ? txt(`  ·  ${stamp(when)}`) : null,
      (p.tags || []).length ? txt(`  ·  ${p.tags.join(', ')}`) : null),
    h('div', { cls: 'item-tools' }, edit, drop, note));
}

async function loadBlog() {
  let data;
  try {
    data = await api('/blog', auth());
  } catch (e) {
    el('blog-empty').hidden = false;
    el('blog-empty').textContent = e.message;
    return;
  }

  const box = el('blog-list');
  box.textContent = '';
  for (const p of data.posts) box.append(blogRow(p, loadBlog));
  el('blog-empty').hidden = data.posts.length > 0;

  const live = data.counts.published || 0;
  const drafts = data.counts.draft || 0;
  const counts = el('blog-counts');
  counts.textContent = '';
  counts.append(h('span', { cls: 'count' }, txt(`${t('blog.live')} `), h('b', { text: String(live) })));
  counts.append(h('span', { cls: 'count' }, txt(`${t('blog.draft')} `), h('b', { text: String(drafts) })));
  el('badge-blog').textContent = drafts ? String(drafts) : '';
}

/* One tab per language in BLOG_LANGS, built here rather than written in the
   HTML, so the tabs, the marks and the panes can never disagree about how many
   languages a post has. They did, for one deploy, and the mismatch threw at
   boot and took the login form down with everything else. */
el('lang-tabs').append(...BLOG_LANGS.map((lang, i) => {
  const tab = h('button', { cls: 'tab', attr: { type: 'button' }, data: { lang } },
    txt(`${lang.toUpperCase()} `), h('b', { cls: 'lang-mark', attr: { id: `mark-${lang}` } }));
  if (i === 0) tab.dataset.on = '1';
  tab.addEventListener('click', () => showLang(lang));
  return tab;
}));
el('lang-panes').append(...BLOG_LANGS.map(pane));
el('b-slugs').addEventListener('click', writeSlugs);
el('b-new').addEventListener('click', () => fillEditor(null));
fillEditor(null);

el('blog-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  el('b-error').hidden = true;
  el('b-note').textContent = '';
  const save = el('b-save');
  save.disabled = true;

  const texts = {};
  for (const lang of BLOG_LANGS) {
    texts[lang] = {
      title: el(`b-title-${lang}`).value,
      lede: el(`b-lede-${lang}`).value,
      slug: el(`b-slug-${lang}`).value,
      tags: el(`b-tags-${lang}`).value,
      body: el(`b-body-${lang}`).value,
      machine: el(`b-machine-${lang}`).checked,
    };
  }

  try {
    const saved = await post('/blog/save', {
      id: editing,
      slug: el('b-slug').value,
      status: el('b-status').value,
      origin: el('b-origin').value,
      texts,
    }, auth());
    editing = saved.id;
    el('editor-slug').textContent = `#${saved.pid}`;
    el('editor-bar').textContent = t('adm.blog_editing');
    el('b-note').textContent = t(saved.status === 'published'
      ? 'adm.blog_saved_live' : 'adm.blog_saved_draft');
    const open = el('b-open');
    open.hidden = saved.status !== 'published';
    if (!open.hidden) open.href = publicUrl(saved.url);
    loadBlog();
  } catch (err) {
    el('b-error').hidden = false;
    el('b-error').textContent = err.message;
  }
  save.disabled = false;
});

/* ── The count ──────────────────────────────────────────────────────
   Two halves, drawn as two panels with nothing between them, because there is
   nothing between them: the api answers with a visitor side and a profile side
   that share no key, and this page could not join them if it tried.

   Bars, not charts. Forty of anything does not need axes, and a chart would
   lend an experimental number a settledness it has not earned. */

const CEN_CLASSES = ['visitor', 'ai', 'search', 'tool', 'scanner', 'unknown'];

function barRow(label, value, max, conf) {
  // A floor of 2%, so a count of one is a visible mark rather than an empty
  // track that reads as zero.
  const pct = max > 0 ? Math.max(2, Math.round((value / max) * 100)) : 0;
  return h('div', { cls: 'bar-row', data: conf ? { conf } : {} },
    h('span', { text: label, attr: { title: label } }),
    h('div', { cls: 'bar-track' },
      h('div', { cls: 'bar-fill', style: { width: `${pct}%` } })),
    h('b', { text: String(value) }));
}

function fillBars(node, pairs, conf) {
  node.textContent = '';
  const max = pairs.reduce((m, [, v]) => Math.max(m, v), 0);
  for (const [label, value] of pairs) {
    node.append(barRow(label, value, max, conf && conf[label]));
  }
}

async function loadCensus() {
  let d;
  try {
    d = await api('/census', auth());
  } catch (e) {
    el('cen-empty').hidden = false;
    el('cen-empty').textContent = e.message;
    return;
  }

  // The one failure the panel has to shout about: with no seed set, every
  // restart forgets who came back, so the whole recurring half is fiction.
  const eph = el('cen-ephemeral');
  eph.hidden = !d.ephemeral;
  if (d.ephemeral) eph.textContent = t('adm.cen_ephemeral');

  const began = new Date(d.epoch_began * 1000).toLocaleDateString();
  el('cen-window').textContent = `${d.window_days}d · ${began}`;

  const counts = el('cen-counts');
  counts.textContent = '';
  const pair = (key, value) => counts.append(h('span', { cls: 'count' },
    txt(`${t(key)} `), h('b', { text: String(value) })));
  pair('adm.cen_seen', d.seen);
  pair('adm.cen_returning', d.returning);
  pair('adm.cen_hits', d.hits);
  pair('adm.cen_live', d.live);

  // Kind, in a fixed order rather than by size: the shape of the traffic is
  // easier to read week to week when the rows do not move around.
  fillBars(el('cen-classes'),
    CEN_CLASSES.filter((c) => d.by_class[c]).map((c) => [t(`cen.class.${c}`), d.by_class[c]]));

  // Region, and country when a region never arrived. Sorted by size, capped:
  // a long tail of ones is noise at this volume.
  const origins = Object.entries(d.by_region)
    .map(([k, v]) => [k.replace(/\/\?$/, ''), v])
    .sort((a, b) => b[1] - a[1])
    .slice(0, 12);
  fillBars(el('cen-origins'), origins);

  const totals = d.subject_totals.slice(0, 20);
  fillBars(el('cen-subjects'), totals.map((s) => [s.steamid, s.hits]));
  el('cen-subject-total').textContent = String(d.subject_totals.length);

  const perDay = Object.entries(d.per_day).sort((a, b) => a[0].localeCompare(b[0]));
  fillBars(el('cen-per-day'), perDay);
  el('cen-empty').hidden = totals.length > 0;

  const hist = (d.history || []).map((row) => {
    const when = new Date(row.began_at * 1000).toLocaleDateString();
    return [when, row.visitors];
  });
  fillBars(el('cen-history'), hist);
  el('cen-history-n').textContent = String((d.history || []).length);
}

/* ── The inbox ─────────────────────────────────────────────────────── */

async function load() {
  let data;
  try {
    data = await api('/inbox', auth());
  } catch (e) {
    if (e.status === 401) {
      show(e.message);
      return;
    }
    el('inbox-title').textContent = e.message;
    return;
  }

  el('gate').hidden = true;
  el('inbox').hidden = false;
  el('logout').hidden = false;

  const fresh = data.counts.novo || 0;
  el('inbox-title').textContent = fresh
    ? (fresh === 1 ? t('adm.new_one') : t('adm.new', { n: fresh }))
    : t('adm.nothing_new');

  const counts = el('counts');
  counts.textContent = '';
  counts.append(h('span', { cls: 'count' }, txt(`${t('adm.total')} `), h('b', { text: String(data.total) })));
  for (const st of STATES) {
    if (!data.counts[st]) continue;
    counts.append(h('span', { cls: 'count' }, txt(`${t(`state.${st}`)} `), h('b', { text: String(data.counts[st]) })));
  }

  const box = el('triage');
  box.textContent = '';
  el('inbox-empty').hidden = data.messages.length > 0;
  for (const m of data.messages) box.append(itemFor(m, load));
  el('badge-mail').textContent = data.messages.length ? String(data.messages.length) : '';

  // Its own request and its own failure. Blocks are the newer half of this
  // page and feedback is the half that has to work: an api that cannot answer
  // for one should still show the other rather than blanking the screen.
  let blocks = { items: [] };
  try {
    blocks = await api('/blocks', auth());
  } catch (e) {
    el('blocks-empty').hidden = false;
    el('blocks-empty').textContent = e.message;
  }

  // Two piles, not one: whoever wrote something, and everybody else. The first
  // is a list of things to answer and the second is a number.
  const spoke = blocks.items.filter(asked);
  const quiet = blocks.items.filter((b) => !asked(b));

  const shut = el('blocks');
  shut.textContent = '';
  for (const entry of spoke) shut.append(blockFor(entry, load));
  if (quiet.length) shut.append(quietBlocks(quiet, load));
  el('blocks-empty').hidden = blocks.items.length > 0;

  const open = blocks.items.filter((b) => b.ban && b.ban.active).length;
  const again = blocks.items.filter((b) => b.ban && b.ban.repeat).length;
  const waiting = blocks.items.filter(
    (b) => b.appeals.some((m) => m.status === 'novo')).length;

  const bc = el('block-counts');
  bc.textContent = '';
  bc.append(h('span', { cls: 'count' }, txt(`${t('adm.block_open_n')} `), h('b', { text: String(open) })));
  if (again) {
    bc.append(h('span', { cls: 'count', data: { warn: '1' } },
      txt(`${t('adm.block_repeat_n')} `), h('b', { text: String(again) })));
  }
  if (waiting) {
    bc.append(h('span', { cls: 'count' },
      txt(`${t('adm.block_waiting')} `), h('b', { text: String(waiting) })));
  }

  // The badge shows the repeat count when there is one, because that is the
  // number worth crossing the page for, and otherwise how many people are
  // waiting on an answer. Not how many addresses are shut out: that number is
  // mostly scanners, it is never zero, and a badge that is always lit is a
  // badge nobody reads. It stays in the counts line above, where it belongs.
  el('badge-block').textContent = again ? `!${again}` : (waiting ? String(waiting) : '');

  // Its own request again, and its own failure: the blog is the newest third
  // of this panel and neither of the other two should go dark with it.
  loadBlog();
}

function show(message) {
  el('gate').hidden = false;
  el('change').hidden = true;
  el('inbox').hidden = true;
  el('logout').hidden = true;
  if (message) {
    el('gate-error').hidden = false;
    el('gate-error').textContent = message;
  }
}

/* The password screen is a gate, not a setting: nothing else is reachable until
   the one that was handed over on paper is gone. */
function askForNewPassword() {
  el('gate').hidden = true;
  el('change').hidden = false;
  el('inbox').hidden = true;
  el('logout').hidden = false;
  el('cur-pass').focus();
}

el('gate-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  el('gate-error').hidden = true;
  const user = el('user').value.trim();
  const password = el('password').value;
  if (!user || !password) return;
  try {
    const r = await post('/login', { user, password });
    el('password').value = '';
    if (r.must_change) askForNewPassword(); else load();
  } catch (err) {
    el('gate-error').hidden = false;
    el('gate-error').textContent = err.message;
  }
});

el('change-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  el('change-error').hidden = true;
  try {
    await post('/password', {
      current: el('cur-pass').value,
      new: el('new-pass').value,
    });
    el('cur-pass').value = '';
    el('new-pass').value = '';
    el('change').hidden = true;
    load();
  } catch (err) {
    el('change-error').hidden = false;
    el('change-error').textContent = err.message;
  }
});

el('logout').addEventListener('click', async () => {
  try { await post('/logout', {}); } catch { /* já era, some com a tela mesmo assim */ }
  show(null);
});

applyStatic();
langSwitchInto(el('langs'));

/* A reload inside the session window lands straight in the inbox. */
api('/me').then((me) => {
  publicSite = (me.public_site_url || '').replace(/\/$/, '');
  document.querySelector('.wordmark').href = publicUrl('/');
  document.querySelector('[data-i18n="nav.public_board"]').href = publicUrl('/feedback');
  if (!me.signed_in) return show(null);
  if (me.must_change) return askForNewPassword();
  load();
}).catch(() => show(null));
