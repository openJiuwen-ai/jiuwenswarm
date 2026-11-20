import { parseArgs } from 'node:util'
import { realpath } from 'node:fs/promises'
import { randomUUID } from 'node:crypto'

const { values } = parseArgs({
  options: {
    entry: { type: 'string' },
    'session-id': { type: 'string' },
    version: { type: 'string' },
    'gateway-url': { type: 'string', default: process.env.WORKSWARM_GATEWAY_API_URL },
    confirmed: { type: 'boolean' },
  },
})

async function publish() {
  if (!values.confirmed || !values.entry || !values['session-id'] || !values['gateway-url']) {
    throw new Error('Specify the confirmed application entry, current session ID and active WebChannel URL.')
  }
  const address = new URL(values['gateway-url'])
  if (!['ws:', 'wss:'].includes(address.protocol) ||
      !['localhost', '127.0.0.1', '[::1]'].includes(address.hostname) ||
      address.username || address.password || address.search || address.hash) {
    throw new Error('Use the current local WorkSwarm WebChannel URL.')
  }
  const entry = await realpath(values.entry)
  const socket = new WebSocket(address)
  const listId = randomUUID()
  const publishId = randomUUID()
  let phase = 'connect to WebChannel'
  const send = (id, method, params) => socket.send(JSON.stringify({ type: 'req', id, method, params }))
  let timer
  try {
    return await new Promise((resolve, reject) => {
      timer = setTimeout(() => reject(new Error('Timed out waiting to ' + phase)), 60000)
      socket.addEventListener('error', () => reject(new Error('WebSocket failed while trying to ' + phase)))
      socket.addEventListener('close', () => reject(new Error('Gateway closed while trying to ' + phase)))
      socket.addEventListener('message', event => {
        try {
          const frame = JSON.parse(event.data)
          if (frame.event === 'connection.ack' && phase === 'connect to WebChannel') {
            phase = 'verify the application market'
            send(listId, 'app_market.list', {})
          } else if (frame.type === 'res' && frame.id === listId && phase === 'verify the application market') {
            if (!frame.ok) throw new Error('Application market unavailable at the supplied WebChannel: ' + frame.error)
            phase = 'publish the application'
            const params = { entry_path: entry, source_session: values['session-id'] }
            if (values.version) params.version = values.version
            send(publishId, 'app_market.publish', params)
          } else if (frame.type === 'res' && frame.id === publishId) {
            if (!frame.ok || !frame.payload?.app_id) throw new Error(frame.error || 'Application publication failed.')
            resolve(frame.payload)
          }
        } catch (error) {
          reject(error)
        }
      })
    })
  } finally {
    clearTimeout(timer)
    socket.close()
  }
}

try {
  console.log(JSON.stringify(await publish()))
} catch (error) {
  console.error(error.message)
  process.exitCode = 1
}
