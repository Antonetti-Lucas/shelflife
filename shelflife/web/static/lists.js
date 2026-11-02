// Drag the games of a list (templates/list.html) into a new order; the order is saved in the
// background and the rank numbers follow. Without this script the arrows on each game still work.
(function () {
  var box = document.querySelector("[data-order-url]");
  if (!box) return;
  var status = document.querySelector(".list-status");
  var dragged = null;
  var before = "";

  function items() { return Array.prototype.slice.call(box.querySelectorAll(".list-item")); }
  function order() { return items().map(function (item) { return item.dataset.appId; }).join(","); }

  function renumber() {
    var all = items();
    all.forEach(function (item, i) {
      var rank = item.querySelector(".rank");
      if (rank) rank.textContent = i + 1;
    });
    // The arrows posted fixed places; after a drag they would be wrong, so they go until a reload.
    box.classList.add("dragged");
  }

  function say(text, bad) {
    if (!status) return;
    status.textContent = text;
    status.classList.toggle("bad", !!bad);
  }

  function save() {
    var now = order();
    if (now === before) return;
    renumber();
    say("Saving…");
    fetch(box.dataset.orderUrl, {
      method: "POST",
      body: new URLSearchParams({ order: now }),
      credentials: "same-origin",
    }).then(function (response) {
      if (!response.ok) throw new Error();
      say("Saved");
    }).catch(function () {
      say("Couldn't save the new order. Reload the page and try again.", true);
    });
  }

  box.addEventListener("dragstart", function (e) {
    var item = e.target.closest && e.target.closest(".list-item");
    if (!item) return;
    dragged = item;
    before = order();
    item.classList.add("dragging");
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", item.dataset.appId);
  });

  box.addEventListener("dragover", function (e) {
    if (!dragged) return;
    e.preventDefault();
    var over = e.target.closest && e.target.closest(".list-item");
    if (!over || over === dragged) return;
    var r = over.getBoundingClientRect();
    over[e.clientX - r.left > r.width / 2 ? "after" : "before"](dragged);
  });

  box.addEventListener("drop", function (e) { if (dragged) e.preventDefault(); });

  box.addEventListener("dragend", function () {
    if (!dragged) return;
    dragged.classList.remove("dragging");
    dragged = null;
    save();
  });

  // A game removed in place (inline.js): the games after it move up.
  new MutationObserver(function () { if (!dragged) renumber(); }).observe(box, { childList: true });
})();
