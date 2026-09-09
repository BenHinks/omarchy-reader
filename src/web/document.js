// Scroll-based reader for normalized TXT, Markdown, and HTML documents.
const viewport = document.querySelector('#viewport')
const slider = document.querySelector('#progress-slider')
const previous = document.querySelector('#previous')
const next = document.querySelector('#next')
let seeking = false
let restoring = true
let updatePending = false
let reportTimer = null

const maximumScroll = () => Math.max(0, viewport.scrollHeight - viewport.clientHeight)

const send = payload => {
  if (globalThis.webkit?.messageHandlers?.reader)
    globalThis.webkit.messageHandlers.reader.postMessage(payload)
}

const reportProgress = (fraction, immediate = false) => {
  if (reportTimer !== null) clearTimeout(reportTimer)
  const report = () => {
    reportTimer = null
    send({ type: 'progress', cfi: null, section: null, fraction })
  }
  if (immediate) report()
  else reportTimer = setTimeout(report, 120)
}

const updateProgress = (report = true, immediate = false) => {
  const maximum = maximumScroll()
  const fraction = maximum ? viewport.scrollTop / maximum : 1
  const normalized = Math.min(1, Math.max(0, fraction))
  const percent = Math.round(normalized * 100)
  document.querySelector('#progress-fill').style.width = `${normalized * 100}%`
  slider.value = String(normalized)
  document.querySelector('#progress-label').textContent = `${percent}%`
  slider.setAttribute('aria-valuetext', `${percent}%`)
  previous.disabled = viewport.scrollTop <= 1
  next.disabled = viewport.scrollTop >= maximum - 1
  if (report) reportProgress(normalized, immediate)
}

const movePage = direction => viewport.scrollBy({
  top: direction * viewport.clientHeight * .9,
  behavior: 'smooth',
})

viewport.addEventListener('scroll', () => {
  if (restoring || seeking || updatePending) return
  updatePending = true
  requestAnimationFrame(() => {
    updatePending = false
    updateProgress()
  })
})
const beginSeek = () => {
  seeking = true
  // Range input must move immediately; otherwise smooth scrolling reads the
  // previous offset and snaps the slider back before the seek can finish.
  viewport.style.scrollBehavior = 'auto'
}

const finishSeek = () => {
  if (!seeking) return
  viewport.style.removeProperty('scroll-behavior')
  seeking = false
  updateProgress(true, true)
}

slider.addEventListener('pointerdown', beginSeek)
slider.addEventListener('input', () => {
  beginSeek()
  viewport.scrollTop = Number(slider.value) * maximumScroll()
  updateProgress()
})
slider.addEventListener('pointerup', finishSeek)
slider.addEventListener('change', finishSeek)
slider.addEventListener('pointercancel', finishSeek)
previous.addEventListener('click', () => movePage(-1))
next.addEventListener('click', () => movePage(1))
document.querySelector('#content').addEventListener('click', event => {
  const link = event.target.closest('a[href]')
  if (!link) return
  event.preventDefault()
  send({ type: 'external-link', href: link.href })
})
document.addEventListener('keydown', event => {
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
    movePage(-1)
  } else if (event.key === 'ArrowRight' || event.key === 'PageDown' || event.key === ' ') {
    event.preventDefault()
    movePage(1)
  }
})

window.addEventListener('load', () => {
  const restored = Math.min(1, Math.max(0, Number(document.body.dataset.progress) || 0))
  // CSS smooth scrolling would briefly leave scrollTop at zero and report that
  // value back to GTK, overwriting the saved position before restoration ends.
  viewport.style.scrollBehavior = 'auto'
  viewport.scrollTop = restored * maximumScroll()
  requestAnimationFrame(() => {
    updateProgress(false)
    viewport.style.removeProperty('scroll-behavior')
    requestAnimationFrame(() => {
      restoring = false
      viewport.focus()
      send({ type: 'ready' })
    })
  })
})
