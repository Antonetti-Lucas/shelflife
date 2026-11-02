// The star rating control (templates/_stars.html): shows the rating under the pointer, and
// clicking your current rating again removes it. Saving is static/inline.js's job.
(function () {
  function form(el) { return el.closest("form.starrate"); }

  document.addEventListener("pointerover", function (e) {
    var star = e.target.closest && e.target.closest(".star-hit button");
    if (star) form(star).style.setProperty("--hover", Number(star.value) * 20 + "%");
  });
  document.addEventListener("pointerout", function (e) {
    var hit = e.target.closest && e.target.closest(".star-hit");
    if (hit && !hit.contains(e.relatedTarget)) form(hit).style.removeProperty("--hover");
  });
  document.addEventListener("click", function (e) {
    var star = e.target.closest && e.target.closest(".star-hit button");
    if (!star) return;
    var f = form(star);
    if (star.value === f.dataset.rating) {  // the same rating again: remove it
      e.preventDefault();
      f.requestSubmit(f.querySelector(".star-clear"));
    }
  });

  // Called by inline.js once a rating is saved.
  window.shelflifeStars = function (f, value) {
    f.dataset.rating = value;
    f.style.setProperty("--value", (Number(value) || 0) * 20 + "%");
    f.style.removeProperty("--hover");
    f.querySelectorAll(".star-hit button").forEach(function (b) {
      b.setAttribute("aria-pressed", String(b.value === value));
    });
    f.classList.remove("saved-flash");
    void f.offsetWidth;  // restart the animation
    f.classList.add("saved-flash");
  };
})();
