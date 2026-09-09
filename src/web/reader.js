import './foliate-js/view.js'

// This page is the small bridge between foliate-js and the native GTK shell.
// Query parameters provide the managed book URL, saved position, and theme.

const params = new URLSearchParams(location.search)
const root = document.documentElement
for (const name of ['background', 'foreground', 'accent', 'selection']) {
  const value = params.get(name)
  if (value) root.style.setProperty(`--${name}`, value)
}
const readerFont = params.get('fontFamily')
const readerFontSize = Number(params.get('fontSize'))
if (readerFont) root.style.setProperty('--reader-font', JSON.stringify(readerFont))
if (readerFontSize) root.style.setProperty('--reader-font-size', `${readerFontSize}px`)
root.style.colorScheme = params.get('mode') === 'light' ? 'light' : 'dark'

const send = payload => {
  // WebKit exposes this handler; optional chaining keeps browser debugging safe.
  if (globalThis.webkit?.messageHandlers?.reader)
    globalThis.webkit.messageHandlers.reader.postMessage(payload)
}

const flattenTOC = items => (items ?? []).flatMap(item =>
  [item, ...flattenTOC(item.subitems)])

const makeChapterTicks = view => {
  // Convert nested TOC entries into approximate global progress positions.
  const boundaries = view.getSectionFractions()
  const resolved = flattenTOC(view.book.toc)
    .map(item => ({ item, target: view.resolveNavigation(item.href) }))
    .filter(({ target }) => Number.isInteger(target?.index))
  const groups = new Map()
  for (const entry of resolved) {
    const group = groups.get(entry.target.index) ?? []
    group.push(entry)
    groups.set(entry.target.index, group)
  }

  const ticks = []
  for (const [index, group] of groups) {
    const start = boundaries[index] ?? 0
    const end = boundaries[index + 1] ?? start
    group.forEach((entry, position) => {
      const fraction = start + (end - start) * position / Math.max(1, group.length)
      const marker = document.createElement('span')
      marker.className = 'chapter-mark'
      marker.style.left = `${fraction * 100}%`
      marker.title = entry.item.label
      document.querySelector('#chapter-marks').append(marker)
      ticks.push({ fraction, href: entry.item.href, label: entry.item.label })
    })
  }
  return ticks
}

const bookStyles = `
  :root { color-scheme: ${params.get('mode') || 'dark'};
          background: ${params.get('background')}; color: ${params.get('foreground')}; }
  body { color: ${params.get('foreground')} !important; background: ${params.get('background')} !important;
         font-family: ${JSON.stringify(params.get('fontFamily') || 'monospace')} !important;
         font-size: ${Number(params.get('fontSize')) || 12}px !important;
         line-height: 1.55; padding-left: 4%; padding-right: 4%; }
  a { color: ${params.get('accent')} !important; }
  ::selection { background: ${params.get('selection')}; color: ${params.get('foreground')}; }
  img, svg { max-width: 100%; height: auto; }
  pre { white-space: pre-wrap; }
`

try {
  // foliate-view handles format parsing and pagination inside the WebKit view.
  const bookHost = document.querySelector('#book')
  const view = document.createElement('foliate-view')
  bookHost.append(view)
  await view.open(params.get('book'))
  view.renderer.setAttribute('flow', 'paginated')
  view.renderer.setStyles?.(bookStyles)
  const handleKeydown = event => {
    if (event.key === 'F1' || (event.ctrlKey && event.key.toLowerCase() === 'g')) {
      event.preventDefault()
      send({ type: 'show-help' })
      return
    }
    if (event.key === 'Escape') {
      event.preventDefault()
      send({ type: 'back' })
      return
    }
    if (event.ctrlKey || event.altKey || event.metaKey
        || event.target.matches?.('button, input, textarea, select')
        || event.target.isContentEditable) return
    if (event.key === 'ArrowLeft' || event.key === 'PageUp' || (event.key === ' ' && event.shiftKey)) {
      event.preventDefault()
      view.goLeft()
    } else if (event.key === 'ArrowRight' || event.key === 'PageDown' || event.key === ' ') {
      event.preventDefault()
      view.goRight()
    }
  }
  view.addEventListener('load', ({ detail: { doc } }) => {
    const style = doc.createElement('style')
    style.textContent = bookStyles
    doc.head.append(style)
    doc.addEventListener('keydown', handleKeydown)
  })
  const slider = document.querySelector('#progress-slider')
  let chapterTicks = []
  let seeking = false
  let pointerSeeking = false
  view.addEventListener('relocate', ({ detail }) => {
    const fraction = Number.isFinite(detail.fraction) ? detail.fraction : 0
    const percent = Math.round(fraction * 100)
    if (!seeking) updateProgress(fraction, detail.tocItem?.label)
    send({ type: 'progress', cfi: detail.cfi ?? null,
      section: detail.section?.current ?? detail.index ?? null, fraction })
  })
  await view.init({ lastLocation: params.get('cfi') || null, showTextStart: true })

  chapterTicks = makeChapterTicks(view)
  const nearestTick = value => {
    const threshold = Math.max(.004, 12 / Math.max(1, slider.clientWidth))
    const nearest = chapterTicks.reduce((best, tick) =>
      !best || Math.abs(tick.fraction - value) < Math.abs(best.fraction - value) ? tick : best, null)
    return nearest && Math.abs(nearest.fraction - value) <= threshold ? nearest : null
  }
  const commitSeek = async () => {
    // Prefer exact chapter destinations when the slider snaps to a TOC marker.
    const value = Number(slider.value)
    const tick = nearestTick(value)
    if (tick) {
      const destination = await view.goTo(tick.href)
      if (!destination) await view.goToFraction(tick.fraction)
    } else await view.goToFraction(value)
    seeking = false
  }
  slider.addEventListener('pointerdown', () => {
    seeking = true
    pointerSeeking = true
  })
  slider.addEventListener('input', () => {
    seeking = true
    const requested = Number(slider.value)
    const tick = nearestTick(requested)
    const preview = tick?.fraction ?? requested
    if (tick) slider.value = String(preview)
    updateProgress(preview, tick?.label)
  })
  slider.addEventListener('pointerup', async () => {
    pointerSeeking = false
    await commitSeek()
  })
  slider.addEventListener('change', async () => {
    if (!pointerSeeking && seeking) await commitSeek()
  })
  slider.addEventListener('pointercancel', () => {
    seeking = false
    pointerSeeking = false
  })

  document.querySelector('#previous').addEventListener('click', () => view.goLeft())
  document.querySelector('#next').addEventListener('click', () => view.goRight())
  document.addEventListener('keydown', handleKeydown)
  // Start in the reading surface rather than a navigation control so page
  // shortcuts work immediately when GTK gives the WebView keyboard focus.
  bookHost.focus({ preventScroll: true })
  send({ type: 'ready' })
} catch (error) {
  send({ type: 'error', message: error?.message ?? String(error) })
}

function updateProgress(fraction, chapter) {
  // Keep the visible meter, accessible slider text, and label in sync.
  const percent = Math.round(fraction * 100)
  document.querySelector('#progress-fill').style.width = `${fraction * 100}%`
  document.querySelector('#progress-slider').value = String(fraction)
  const description = chapter ? `${percent}% · ${chapter}` : `${percent}%`
  document.querySelector('#progress-label').textContent = description
  document.querySelector('#progress-slider').setAttribute('aria-valuetext', description)
}
