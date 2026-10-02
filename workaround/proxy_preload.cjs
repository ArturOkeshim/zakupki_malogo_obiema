const http = require("http");
const https = require("https");
const http2 = require("http2");
const net = require("net");
const tls = require("tls");
const { Duplex } = require("stream");
const { URL } = require("url");

// ЗАМЕТКА ДЛЯ ОБУЧЕНИЯ: Node сам подставляет HTTPS_PROXY только в fetch.
// Мост ходит в Cursor через http.request, https.request и http2.connect,
// поэтому этот файл подменяет эти вызовы и сначала открывает туннель CONNECT.

function currentProxyUrl() {
  return (
    process.env.HTTPS_PROXY ||
    process.env.https_proxy ||
    process.env.HTTP_PROXY ||
    process.env.http_proxy ||
    ""
  );
}

function shouldSkipProxy(hostname) {
  const rawRules = process.env.NO_PROXY || process.env.no_proxy || "";
  const host = String(hostname || "").toLowerCase();
  const rules = rawRules.split(",");
  for (const ruleText of rules) {
    const rule = ruleText.trim().toLowerCase();
    if (!rule) {
      continue;
    }
    if (rule === "*" || host === rule || host.endsWith("." + rule)) {
      return true;
    }
  }
  return false;
}

function basicAuthHeader(proxy) {
  const user = decodeURIComponent(proxy.username);
  const password = decodeURIComponent(proxy.password);
  const token = Buffer.from(user + ":" + password).toString("base64");
  return "Basic " + token;
}

function proxyAuthorization(proxy) {
  if (!proxy.username) {
    return "";
  }
  return "Proxy-Authorization: " + basicAuthHeader(proxy) + "\r\n";
}

function openTunnelSocket(targetHost, targetPort) {
  const proxy = new URL(currentProxyUrl());
  const tcp = net.connect(Number(proxy.port || 80), proxy.hostname);
  const bufferedWrites = [];
  let headerBuffer = Buffer.alloc(0);
  let tunnelReady = false;

  // TLS пишет сюда сразу. В прокси эти байты уйдут только после ответа 200.
  const tunnel = new Duplex({
    read() {},
    write(chunk, encoding, callback) {
      if (!tunnelReady) {
        bufferedWrites.push([chunk, encoding, callback]);
        return;
      }
      tcp.write(chunk, encoding, callback);
    },
  });

  tcp.on("connect", () => {
    const request =
      "CONNECT " + targetHost + ":" + targetPort + " HTTP/1.1\r\n" +
      "Host: " + targetHost + ":" + targetPort + "\r\n" +
      proxyAuthorization(proxy) +
      "\r\n";
    tcp.write(request);
  });

  tcp.on("data", (chunk) => {
    if (tunnelReady) {
      tunnel.push(chunk);
      return;
    }
    headerBuffer = Buffer.concat([headerBuffer, chunk]);
    const headerEnd = headerBuffer.indexOf("\r\n\r\n");
    if (headerEnd === -1) {
      return;
    }
    const headerText = headerBuffer.slice(0, headerEnd).toString("latin1");
    const statusLine = headerText.split("\r\n")[0];
    const statusCode = Number(statusLine.split(" ")[1]);
    if (statusCode !== 200) {
      tunnel.destroy(new Error("Proxy CONNECT failed: " + statusLine));
      tcp.destroy();
      return;
    }
    tunnelReady = true;
    for (const writeArgs of bufferedWrites) {
      tcp.write(writeArgs[0], writeArgs[1], writeArgs[2]);
    }
    const rest = headerBuffer.slice(headerEnd + 4);
    if (rest.length > 0) {
      tunnel.push(rest);
    }
  });

  tcp.on("error", (error) => tunnel.destroy(error));
  tcp.on("end", () => tunnel.push(null));
  tunnel.on("close", () => tcp.destroy());
  return tunnel;
}

function tlsOverProxy(hostname, port, alpnProtocols) {
  const rawSocket = openTunnelSocket(hostname, port);
  return tls.connect({
    socket: rawSocket,
    servername: hostname,
    ALPNProtocols: alpnProtocols,
  });
}

function readTarget(args, defaultProtocol) {
  const first = args[0];
  if (typeof first === "string" || first instanceof URL) {
    return new URL(first);
  }
  const protocol = first.protocol || defaultProtocol || "https:";
  const hostname = first.hostname || first.host || "localhost";
  const port = first.port || "";
  return new URL(protocol + "//" + hostname + (port ? ":" + port : ""));
}

const httpsProxyAgent = new https.Agent();
httpsProxyAgent.createConnection = function (options, callback) {
  const hostname = options.servername || options.host || options.hostname;
  const port = Number(options.port || 443);
  const socket = tlsOverProxy(hostname, port, ["http/1.1"]);
  socket.once("secureConnect", () => {
    if (callback) {
      callback(null, socket);
    }
  });
  socket.once("error", (error) => {
    if (callback) {
      callback(error);
    }
  });
  return socket;
};

function patchHttpRequest() {
  const originalRequest = http.request;
  http.request = function (...args) {
    let target;
    try {
      target = readTarget(args, "http:");
    } catch (error) {
      return originalRequest.apply(this, args);
    }
    if (!currentProxyUrl() || target.protocol !== "http:" || shouldSkipProxy(target.hostname)) {
      return originalRequest.apply(this, args);
    }
    const proxy = new URL(currentProxyUrl());
    const first = args[0];
    const hasSeparateOptions = typeof first === "string" || first instanceof URL;
    const sourceOptions = hasSeparateOptions && args[1] && typeof args[1] !== "function" ? args[1] : first;
    const options = { ...(typeof sourceOptions === "object" ? sourceOptions : {}) };
    options.hostname = proxy.hostname;
    options.host = proxy.host;
    options.port = proxy.port || 80;
    options.path = target.href;
    const headers = { ...(options.headers || {}) };
    headers.Host = target.host;
    if (proxy.username) {
      headers["Proxy-Authorization"] = basicAuthHeader(proxy);
    }
    options.headers = headers;
    const callbackIndex = hasSeparateOptions && args[1] && typeof args[1] !== "function" ? 2 : 1;
    const callback = typeof args[callbackIndex] === "function" ? args[callbackIndex] : undefined;
    if (callback) {
      return originalRequest.call(this, options, callback);
    }
    return originalRequest.call(this, options);
  };
}

function patchHttpsRequest() {
  const originalRequest = https.request;
  https.request = function (...args) {
    let target;
    try {
      target = readTarget(args, "https:");
    } catch (error) {
      return originalRequest.apply(this, args);
    }
    if (!currentProxyUrl() || target.protocol !== "https:" || shouldSkipProxy(target.hostname)) {
      return originalRequest.apply(this, args);
    }
    const first = args[0];
    if (typeof first === "string" || first instanceof URL) {
      const hasOptions = args[1] && typeof args[1] !== "function";
      const options = hasOptions ? { ...args[1] } : {};
      if (!options.agent) {
        options.agent = httpsProxyAgent;
      }
      const rest = hasOptions ? args.slice(2) : args.slice(1);
      return originalRequest.call(this, first, options, ...rest);
    }
    const options = { ...first };
    if (!options.agent) {
      options.agent = httpsProxyAgent;
    }
    return originalRequest.call(this, options, ...args.slice(1));
  };
}

function patchHttp2Connect() {
  const originalConnect = http2.connect;
  http2.connect = function (authority, options, listener) {
    if (typeof options === "function") {
      listener = options;
      options = undefined;
    }
    let target;
    try {
      target = new URL(authority);
    } catch (error) {
      return originalConnect(authority, options, listener);
    }
    const nextOptions = { ...(options || {}) };
    if (
      currentProxyUrl() &&
      target.protocol === "https:" &&
      !shouldSkipProxy(target.hostname) &&
      !nextOptions.createConnection
    ) {
      const port = Number(target.port || 443);
      nextOptions.createConnection = () => {
        return tlsOverProxy(target.hostname, port, ["h2"]);
      };
    }
    if (listener) {
      return originalConnect(authority, nextOptions, listener);
    }
    return originalConnect(authority, nextOptions);
  };
}

patchHttpRequest();
patchHttpsRequest();
patchHttp2Connect();
