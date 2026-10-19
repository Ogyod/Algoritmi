import test from "node:test";
import assert from "node:assert/strict";
import {interpretDocument} from "../static/browser-processor.js";

test("parser can recover from a timeout, failed initialization and message failure", async (t) => {
  const workers = [];
  class WorkerStub {
    constructor() {workers.push(this);}
    postMessage(message) {
      if (this.failMessage) throw new Error("Cannot clone request");
      this.message = message;
    }
    terminate() {this.terminated = true;}
    reply(result) {this.onmessage({data: {id: this.message.id, result}});}
  }
  const original = globalThis.Worker;
  globalThis.Worker = WorkerStub;
  t.mock.timers.enable({apis: ["setTimeout"]});
  try {
    const timedOut = assert.rejects(interpretDocument({lines: [], type: "auto"}), /слишком много времени/);
    t.mock.timers.tick(300000);
    await timedOut;
    assert.equal(workers[0].terminated, true);

    const recovered = interpretDocument({lines: [], type: "auto"});
    workers[1].reply({type: "unknown"});
    assert.deepEqual(await recovered, {type: "unknown"});

    const failedInit = assert.rejects(interpretDocument({lines: [], type: "auto"}), /не загрузился/);
    workers[1].onmessage({data: {id: workers[1].message.id, error: "Обработчик не загрузился"}});
    await failedInit;
    assert.equal(workers[1].terminated, true);

    const restarted = interpretDocument({lines: [], type: "auto"});
    workers[2].reply({type: "unknown"});
    await restarted;
    workers[2].failMessage = true;
    await assert.rejects(interpretDocument({lines: [], type: "auto"}), /передать данные/);
    assert.equal(workers[2].terminated, true);

    const finalRequest = interpretDocument({lines: [], type: "auto"});
    workers[3].reply({type: "unknown"});
    await finalRequest;
  } finally {
    for (const worker of workers) worker.terminate();
    globalThis.Worker = original;
    t.mock.timers.reset();
  }
});
