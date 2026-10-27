/* KETS background slideshow — fixes the fixed wallpaper layer without changing app/trading behavior. */
(function () {
  'use strict';

  var slides = [
    '/eric-investments-background.png?v=20261027slide1',
    '/executive-car-background.jpg?v=20261027slide2',
    '/executive-boardroom-background.jpg?v=20261027slide3'
  ];
  var current = -1;
  var timer = null;

  function applySlide(index) {
    if (!document.documentElement || !slides.length) return;
    current = ((index % slides.length) + slides.length) % slides.length;
    // The visible wallpaper is body::before (the body itself is intentionally transparent).
    // Update its CSS variable instead of body.backgroundImage, which is hidden by the fixed pseudo-element.
    document.documentElement.style.setProperty('--kets-slide-image', 'url("' + slides[current] + '")', 'important');
    document.documentElement.setAttribute('data-kets-slide', String(current + 1));
  }

  function start() {
    slides.forEach(function (src) {
      var image = new Image();
      image.onload = function () {};
      image.src = src;
    });
    applySlide(0);
    if (timer) window.clearInterval(timer);
    timer = window.setInterval(function () {
      applySlide(current + 1);
    }, 7000);
    window.addEventListener('pageshow', function () {
      applySlide(current < 0 ? 0 : current);
    });
    document.addEventListener('visibilitychange', function () {
      if (!document.hidden) applySlide(current < 0 ? 0 : current);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start, { once: true });
  } else {
    start();
  }
})();
