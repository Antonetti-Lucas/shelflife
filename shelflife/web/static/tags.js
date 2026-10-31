// Turns each tag box into a picker that holds several tags as removable chips.
// The page works without this: the box is then a plain text field of comma separated tags.
(function () {
  function upgrade(box) {
    var field = box.querySelector("input[name]");
    if (!field) return;
    var listId = field.getAttribute("list");
    var placeholder = field.getAttribute("placeholder") || "";
    var tags = [];

    var chips = document.createElement("span");
    chips.className = "chips";
    var entry = document.createElement("input");
    entry.type = "search";
    entry.className = "tagtext";
    entry.setAttribute("autocomplete", "off");
    entry.setAttribute("aria-label", field.getAttribute("aria-label") || "Add a tag");
    if (listId) entry.setAttribute("list", listId);

    function has(name) {
      return tags.some(function (t) { return t.toLowerCase() === name.toLowerCase(); });
    }

    function render() {
      field.value = tags.join(",");
      chips.textContent = "";
      tags.forEach(function (name, i) {
        var chip = document.createElement("span");
        chip.className = "chip";
        chip.textContent = name;
        var remove = document.createElement("button");
        remove.type = "button";
        remove.className = "chipx";
        remove.textContent = "×";
        remove.setAttribute("aria-label", "Remove " + name);
        remove.addEventListener("click", function () {
          tags.splice(i, 1);
          render();
          entry.focus();
        });
        chip.appendChild(remove);
        chips.appendChild(chip);
      });
      entry.placeholder = tags.length ? "Add another tag" : placeholder;
    }

    function add(raw) {
      raw.split(",").forEach(function (part) {
        var name = part.trim();
        if (name && !has(name)) tags.push(name);
      });
      render();
    }

    function takeEntry() {
      if (entry.value.trim()) add(entry.value);
      entry.value = "";
    }

    field.type = "hidden";
    field.removeAttribute("list");
    field.removeAttribute("placeholder");
    box.classList.add("enhanced");
    box.appendChild(chips);
    box.appendChild(entry);
    add(field.value);

    entry.addEventListener("input", function (e) {
      // A comma ends a tag; so does choosing a suggestion (not plain typing).
      var picked = typeof InputEvent !== "undefined"
        ? !(e instanceof InputEvent) || e.inputType === "insertReplacementText"
        : false;
      if (entry.value.indexOf(",") !== -1 || (picked && entry.value.trim())) takeEntry();
    });
    entry.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && entry.value.trim()) {
        e.preventDefault();
        takeEntry();
      } else if (e.key === "Backspace" && !entry.value && tags.length) {
        tags.pop();
        render();
      }
    });
    // Text left in the box becomes a tag when the form is sent, not when the box loses
    // focus: a chip appearing then would shift the layout and swallow the click on Apply.
    box.addEventListener("click", function (e) {
      if (e.target === box) entry.focus();
    });
    if (field.form) field.form.addEventListener("submit", takeEntry);
  }

  document.querySelectorAll("[data-tagpicker]").forEach(upgrade);
})();
