// Service worker. The only place that talks to the local scoring server.
//
// Content scripts run in the page's origin, so routing requests through here
// keeps the fetch off the page and gives one place to handle the server being
// down.

const SERVER = "http://127.0.0.1:8765";

async function post(path, payload) {
  const response = await fetch(`${SERVER}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  return response.json();
}

chrome.runtime.onMessage.addListener((message, _sender, respond) => {
  const routes = {
    score: "/score",
    "generate-resume": "/resume",
    "draft-message": "/message",
  };
  const path = routes[message.type];
  if (!path) return false;

  post(path, message.payload)
    .then(respond)
    .catch((err) => {
      // A refused connection almost always means the server is not running,
      // which is worth saying plainly rather than surfacing a fetch error.
      respond({ ok: false, unreachable: true, error: String(err) });
    });

  return true; // keep the message channel open for the async reply
});
