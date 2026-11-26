import { appendFileSync } from "node:fs";
import { createServer } from "node:http2";

const server = createServer();
let cacheRequests = 0;

server.on("stream", (stream, headers) => {
  stream.on("error", () => {});
  const path = headers[":path"];
  if (path === "/proxy/cancel") {
    appendFileSync(process.argv[3], "opened\n");
    stream.on("close", () => appendFileSync(process.argv[3], "closed\n"));
    return;
  }
  if (path === "/proxy/drain") {
    stream.respond({ ":status": 200, "content-type": "text/plain" });
    stream.write("before-");
    setTimeout(() => stream.end("after"), 200);
    return;
  }
  if (path === "/proxy/cache") {
    stream.respond({ ":status": 200, "content-type": "text/plain", "cache-control": "public, max-age=60" });
    stream.end(`origin ${++cacheRequests}`);
    return;
  }
  let trailer = "missing";
  stream.respond({ ":status": 200, "content-type": "text/plain" }, { waitForTrailers: true });
  stream.on("data", (chunk) => stream.write(chunk.toString().toUpperCase()));
  stream.on("trailers", (headers) => { trailer = headers["x-upload-trailer"]; });
  stream.on("wantTrailers", () => stream.sendTrailers({ "x-origin-trailer": trailer, "x-origin-protocol": "h2" }));
  stream.on("end", () => stream.end());
});
server.listen(Number(process.argv[2]), "127.0.0.1");
