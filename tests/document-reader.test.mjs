import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'


const makeElement = () => ({
  disabled: false,
  listeners: {},
  style: {
    removeProperty(name) {
      if (name === 'scroll-behavior') delete this.scrollBehavior
    },
  },
  value: '0',
  addEventListener(name, callback) { this.listeners[name] = callback },
  focus() {},
  setAttribute() {},
})


test('EPUB reader initially focuses its reading surface', () => {
  const html = readFileSync(new URL('../src/web/reader.html', import.meta.url), 'utf8')
  const script = readFileSync(new URL('../src/web/reader.js', import.meta.url), 'utf8')
  assert.match(html, /<main id="book" tabindex="0"/)
  assert.match(script, /bookHost\.focus\(/)
  assert.doesNotMatch(script, /querySelector\('#previous'\)\.focus\(/)
})


test('reflowable EPUB typography overrides publisher body text styles', () => {
  const script = readFileSync(new URL('../src/web/reader.js', import.meta.url), 'utf8')

  assert.match(script, /font-size: 1rem !important;/)
  assert.match(script, /body :where\(\*\) \{ font-family: inherit !important; \}/)
  assert.match(script, /body :where\(p, li, dt, dd, blockquote, figcaption, table\)/)
  assert.match(script, /if \(!view\.isFixedLayout\) view\.renderer\.setStyles/)
  assert.match(script, /if \(!view\.isFixedLayout\) \{/)
  assert.match(script, /contentFontSize.*readerFontSize.*12/)
  assert.match(script, /font-size: \$\{contentFontSize\}px !important;/)
})


test('document text sizing is separate from reader controls', () => {
  const styles = readFileSync(new URL('../src/web/document.css', import.meta.url), 'utf8')

  assert.match(styles, /body \{ font-family: var\(--reader-font\); font-size: var\(--reader-font-size\); \}/)
  assert.match(styles, /#content[\s\S]*font-size: var\(--content-font-size\);/)
})


test('restores progress and saves slider seeks', async () => {
  const windowListeners = {}
  const documentListeners = {}
  const pageMoves = []
  const viewport = Object.assign(makeElement(), {
    clientHeight: 100,
    scrollHeight: 1000,
    scrollTop: 0,
    scrollBy(options) { pageMoves.push(options.top) },
  })
  const elements = {
    '#viewport': viewport,
    '#progress-slider': makeElement(),
    '#previous': makeElement(),
    '#next': makeElement(),
    '#content': makeElement(),
    '#progress-fill': makeElement(),
    '#progress-label': makeElement(),
  }
  const messages = []

  globalThis.document = {
    body: { dataset: { progress: '0.42' } },
    addEventListener(name, callback) { documentListeners[name] = callback },
    querySelector(selector) { return elements[selector] },
  }
  globalThis.window = {
    addEventListener(name, callback) { windowListeners[name] = callback },
  }
  globalThis.requestAnimationFrame = callback => callback()
  globalThis.webkit = {
    messageHandlers: { reader: { postMessage(message) { messages.push(message) } } },
  }

  await import('../src/web/document.js')
  windowListeners.load()

  assert.equal(viewport.scrollTop, 378)
  assert.equal(messages.some(message => message.type === 'progress'), false)

  const slider = elements['#progress-slider']
  slider.value = '0.65'
  slider.listeners.input()
  assert.equal(viewport.scrollTop, 585)
  assert.equal(slider.value, '0.65')

  slider.listeners.pointerup()
  assert.equal(
    messages.filter(message => message.type === 'progress').at(-1)?.fraction,
    0.65,
  )

  const keydown = documentListeners.keydown
  const keyEvent = (key, overrides = {}) => ({
    key,
    ctrlKey: false,
    altKey: false,
    metaKey: false,
    shiftKey: false,
    target: { matches: () => false },
    preventDefault() {},
    ...overrides,
  })
  keydown(keyEvent(' ', { shiftKey: true }))
  keydown(keyEvent('ArrowRight'))
  assert.deepEqual(pageMoves, [-90, 90])

  keydown(keyEvent('Escape'))
  keydown(keyEvent('g', { ctrlKey: true }))
  assert.deepEqual(messages.slice(-2), [{ type: 'back' }, { type: 'show-help' }])

  keydown(keyEvent('ArrowRight', { target: { matches: () => true } }))
  assert.deepEqual(pageMoves, [-90, 90])
})
