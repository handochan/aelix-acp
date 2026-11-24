// Drive Studio's renderer client against the protected Host from the ACP probe.
import assert from 'node:assert/strict';
import { writeFile } from 'node:fs/promises';
import { createInterface } from 'node:readline';
import { registerDesktopBackendFetcher } from '@/common/adapter/desktopBackendFetch';
import { createRunningInputClient } from '@renderer/services/runtime/RunningInput';
import { INPUT_METHODS } from '@renderer/services/runtime/RunningInput/transport';

console.debug = console.error;
const input = createInterface({ input: process.stdin });
const commands = input[Symbol.asyncIterator]();
const { port, token, conversationId, requestId, path } = JSON.parse((await commands.next()).value!);
const origin = 'http://127.0.0.1:' + port;
const image = await Bun.file(path).arrayBuffer();
const abortHttp = new AbortController();
let grant: { id: string; sha256: string } | undefined;
let writes = 0;
let replyReceived = false;
let bindingId: string;
const send = (event: object) => process.stdout.write(JSON.stringify(event) + '\n');

registerDesktopBackendFetcher(async (url, init) => {
  const target = new URL(url);
  const headers = new Headers(init?.headers);
  headers.set('x-aionui-studio-auth', token);
  const payload = typeof init?.body === 'string' ? JSON.parse(init.body) : {};
  const write = payload.method === INPUT_METHODS.steerImages;
  if (write || payload.method === INPUT_METHODS.lookup) {
    assert.equal(payload.bindingId, bindingId);
    assert.equal(payload.payload.requestId, requestId);
  }
  if (write) writes++;
  const options = {
    ...init, headers,
    ...(write && { signal: AbortSignal.any([init!.signal!, abortHttp.signal]) }),
  };
  const endpoint = origin + target.pathname + target.search;
  const response = await fetch(endpoint, options);
  if (write) replyReceived = true;
  if (target.pathname.endsWith('/byte-grants')) {
    const { data } = await response.clone().json();
    assert.ok(response.ok && data?.grants?.length === 1);
    grant = data.grants[0];
    // The subsequent grant read must still use the immutable prepared bytes.
    await writeFile(path, 'changed after grant');
    const replay = await fetch(endpoint, options);
    assert.ok(replay.ok);
    const { data: again } = await replay.json();
    assert.deepEqual(data.grants, again.grants);
    assert.equal(data.expiresAtMs, again.expiresAtMs);
  }
  return response;
});

const client = createRunningInputClient(conversationId, { enablePixels: true });
try {
  await client.refresh();
  const { channel } = client.getSnapshot();
  assert.ok(channel.status === 'ready' && channel.turn.active && channel.pixelModes?.includes('steer'));
  bindingId = channel.bindingId;
  const request = client.submitImages({
    bindingId,
    turnId: channel.turn.turnId,
    mode: 'steer',
    requestId,
    message: '',
    sources: [{ ref: { kind: 'local', path }, blob: new Blob([image]) }],
    signal: new AbortController().signal,
    isCurrent: () => true,
  });
  const cancel = await Promise.race([
    commands.next(),
    request.then((result) => {
      const submission = client.getSnapshot().imageSubmission;
      const reason = submission?.status === 'blocked' ? submission.reason : result.status;
      throw new Error('Image caller settled before HTTP cancellation: ' + reason);
    }),
  ]);
  assert.equal(cancel.value, 'cancel');
  assert.equal(replyReceived, false);
  abortHttp.abort();

  const sent = await request;
  assert.equal(sent.status, 'sent');
  assert.equal(grant?.sha256, client.getSnapshot().receipts[0]?.pixels?.images[0]?.sent.sha256);
  send({ receipt: sent.receipt, preserveDraft: sent.preserveDraft, writes, grant });
  assert.equal((await commands.next()).value, 'lookup');
  send(await client.lookup(requestId));
  assert.equal((await commands.next()).value, 'lookup');
  send({ result: await client.lookup(requestId), writes });
} finally {
  client.dispose();
  input.close();
}
