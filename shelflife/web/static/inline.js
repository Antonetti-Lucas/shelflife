// Saves small edits (a rating, a status, hours, "not interested") without reloading the page.
// A form marked data-inline is sent in the background and the page stays where it is:
//   data-inline         shows "Saved" next to the form (a star rating shows its new stars)
//   data-inline="hide"  hides the card or row it is in, with an Undo
//   data-inline="remove" removes the card or row it is in
//   data-inline="done"  replaces the form with its data-done text (and an "Open it" link to data-done-link)
//   data-inline="toggle" flips an on/off form (its hidden "value", its "on" class, its button's title)
// Without this script, or if the background request fails to reach the app, the form is sent
// the normal way and the page reloads.
(function () {
  function send(form, extra, submitter) {
    var body = new URLSearchParams(new FormData(form));
    // The button that sent the form carries a value too (each half star is one).
    if (submitter && submitter.name) body.set(submitter.name, submitter.value);
    Object.keys(extra || {}).forEach(function (k) { body.set(k, extra[k]); });
    // "manual": the app answers a saved form with a redirect to the page; there is no need to load it.
    return fetch(form.action, { method: "POST", body: body, redirect: "manual", credentials: "same-origin" });
  }

  function ok(response) {
    return response.type === "opaqueredirect" || (response.status >= 200 && response.status < 400);
  }

  function note(form, text, bad) {
    var old = form.querySelector(".saved");
    if (old) old.remove();
    var tag = document.createElement("span");
    tag.className = bad ? "saved bad" : "saved";
    tag.textContent = text;
    tag.setAttribute("role", "status");
    form.appendChild(tag);
    if (!bad) setTimeout(function () { tag.classList.add("gone"); }, 1400);
    setTimeout(function () { tag.remove(); }, bad ? 6000 : 1900);
  }

  function whyNot(response) {
    return response.json().then(
      function (data) { return typeof data.detail === "string" ? data.detail : "Couldn't save"; },
      function () { return "Couldn't save"; }
    );
  }

  // form.submit() leaves out the button that was pressed, so copy its value into the form first.
  function withValue(form, submitter) {
    if (submitter.name) {
      var field = document.createElement("input");
      field.type = "hidden";
      field.name = submitter.name;
      field.value = submitter.value;
      form.appendChild(field);
    }
    return form;
  }

  function toggle(form) {
    var value = form.querySelector('input[name="value"]');
    var nowOn = value.value === "1";  // what was just saved
    value.value = nowOn ? "0" : "1";
    form.classList.toggle("on", nowOn);
    var button = form.querySelector("button");
    button.setAttribute("aria-pressed", String(nowOn));
    button.title = nowOn ? form.dataset.onTitle : form.dataset.offTitle;
  }

  function done(form) {
    var line = document.createElement("p");
    line.className = "meta done-note";
    line.setAttribute("role", "status");
    line.textContent = (form.dataset.done || "Done.") + " ";
    if (form.dataset.doneLink) {
      var link = document.createElement("a");
      link.href = form.dataset.doneLink;
      link.textContent = "Open it";
      line.appendChild(link);
    }
    form.replaceWith(line);
  }

  function holder(form) {
    return form.closest(".card, .row, tr, .list-item");
  }

  function hide(form, item) {
    var name = (item.querySelector("h3") || item).textContent.trim();
    var undo = document.createElement(item.tagName === "TR" ? "tr" : "div");
    undo.className = "undo-note";
    var box = undo;
    if (item.tagName === "TR") {
      box = document.createElement("td");
      box.colSpan = item.children.length;
      undo.appendChild(box);
    }
    var text = document.createElement("span");
    text.textContent = "Not interested: " + name + ". ";
    var button = document.createElement("button");
    button.type = "button";
    button.className = "ghost small";
    button.textContent = "Undo";
    button.addEventListener("click", function () {
      button.disabled = true;
      send(form, { value: "0" }).then(function (response) {
        if (!ok(response)) throw new Error();
        undo.remove();
        item.hidden = false;
      }).catch(function () {
        button.disabled = false;
        text.textContent = "Couldn't undo. ";
      });
    });
    box.appendChild(text);
    box.appendChild(button);
    item.hidden = true;
    item.after(undo);
  }

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form.matches || !form.matches("form[data-inline]") || event.defaultPrevented) return;
    event.preventDefault();
    var how = form.getAttribute("data-inline");
    var submitter = event.submitter;
    var controls = Array.prototype.slice.call(form.elements);
    controls.forEach(function (c) { c.setAttribute("aria-busy", "true"); });
    send(form, null, submitter).then(function (response) {
      controls.forEach(function (c) { c.removeAttribute("aria-busy"); });
      if (!ok(response)) {
        return whyNot(response).then(function (text) { note(form, text, true); });
      }
      var item = holder(form);
      if (how === "hide" && item) hide(form, item);
      else if (how === "remove" && item) item.remove();
      else if (how === "done") done(form);
      else if (how === "toggle") toggle(form);
      else if (form.matches(".starrate") && window.shelflifeStars) window.shelflifeStars(form, submitter ? submitter.value : "");
      else note(form, "Saved");
      form.querySelectorAll("select.stars").forEach(function (s) { s.classList.toggle("unrated", !s.value); });
    }).catch(function () {
      // The app could not be reached in the background: send it the normal way.
      if (submitter) withValue(form, submitter);
      form.submit();
    });
  });
})();
