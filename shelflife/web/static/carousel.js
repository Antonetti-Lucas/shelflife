// Rows of covers paged with arrows: each press moves by nearly a full row of games.
(function () {
  function wire(box) {
    var track = box.querySelector(".car-track");
    var buttons = box.querySelectorAll(".car-btn");
    function update() {
      var max = track.scrollWidth - track.clientWidth - 1;
      buttons[0].disabled = track.scrollLeft <= 0;
      buttons[1].disabled = track.scrollLeft >= max;
      box.classList.toggle("all-fit", max <= 0);
    }
    buttons.forEach(function (b) {
      b.addEventListener("click", function () {
        track.scrollBy({ left: Number(b.dataset.dir) * track.clientWidth * 0.9, behavior: "smooth" });
      });
    });
    track.addEventListener("scroll", update, { passive: true });
    window.addEventListener("resize", update);
    update();
  }
  document.querySelectorAll("[data-carousel]").forEach(wire);
})();
