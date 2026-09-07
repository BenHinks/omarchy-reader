import assert from 'node:assert/strict'
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


test('restores progress and saves slider seeks', async () => {
  const windowListeners = {}
  const viewport = Object.assign(makeElement(), {
    clientHeight: 100,
    scrollHeight: 1000,
    scrollTop: 0,
    scrollBy() {},
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
    addEventListener() {},
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
})
