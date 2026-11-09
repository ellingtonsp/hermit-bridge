// Renders Hermes blobatar faces. Reads [{seed, trait}] as JSON on stdin and prints SVG strings.
import { pathToFileURL } from 'node:url'

const { blobatar } = await import(pathToFileURL(process.argv[2]).href)
let input = ''
for await (const chunk of process.stdin) input += chunk
const faces = JSON.parse(input).map(({ seed, trait }) =>
  blobatar(seed, trait === null ? { size: 256 } : { size: 256, traits: { shape: trait } }))
process.stdout.write(JSON.stringify(faces))
