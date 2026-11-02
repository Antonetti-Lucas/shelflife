// Keeps the prices on screen current: asks the app for the games this page shows (it re-reads
// those older than a few hours from the store, a hundred at a time) and updates them in place.
(function () {
  var tags = Array.prototype.slice.call(document.querySelectorAll("[data-price-id]"));
  if (!tags.length) return;
  var ids = Array.from(new Set(tags.map(function (t) { return t.dataset.priceId; }))).slice(0, 100);

  function fill(tag, p) {
    tag.textContent = "";
    if (!p) return;
    if (p.discount) {
      var off = document.createElement("span");
      off.className = "disc";
      off.textContent = "-" + p.discount + "%";
      tag.appendChild(off);
    }
    if (p.was) {
      var was = document.createElement("s");
      was.textContent = p.was;
      tag.appendChild(was);
    }
    var now = document.createElement("b");
    now.textContent = p.text;
    tag.appendChild(now);
  }

  fetch("/prices?ids=" + ids.join(","), { credentials: "same-origin" })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (prices) {
      if (!prices) return;
      tags.forEach(function (tag) {
        if (tag.dataset.priceId in prices) fill(tag, prices[tag.dataset.priceId]);
      });
    })
    .catch(function () {});  // offline or the app restarting: keep what the page shows
})();
