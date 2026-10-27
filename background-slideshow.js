/* KETS background slideshow — reliable inline background rotation.
   Only changes the page wallpaper; it does not alter app/trading behavior. */
(function () {
  'use strict';

  var slides = [
    '/eric-investments-background.png?v=20261027slide1',
    '/executive-car-background.jpg?v=20261027slide2',
    '/executive-boardroom-background.jpg?v=20261027slide3'
  ];
  var current = -1;
  var timer = null;

  function setImportant(element, property, value) {
    element.style.setProperty(property, value, 'important');
  }

  function applySlide(index) {
    if (!document.body) return;
    current = ((index % slides.length) + slides.length) % slides.length;

    // Apply the wallpaper directly to BODY. Inline !important takes precedence
    // over the legacy stylesheet rules that previously pinned one static image.
    setImportant(document.body, 'background-image',
      'linear-gradient(135deg, rgba(3,8,18,.12), rgba(3,8,18,.22)), url("' +
      slides[current] + '")');
    setImportant(document.body, 'background-position', 'center center');
    setImportant(document.body, 'background-repeat', 'no-repeat');
    setImportant(document.body, 'background-size', 'cover');
    setImportant(document.body, 'background-attachment',
      window.matchMedia && window.matchMedia('(max-width: 700px)').matches ? 'scroll' : 'fixed');
    setImportant(document.body, 'background-color', '#07111f');
  }

  function start() {
    // Preload images without blocking rotation if one image is unavailable.
    slides.forEach(function (src) {
      var image = new Image();
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
      if (!document.hidden && current < 0) applySlide(0);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start, { once: true });
  } else {
    start();
  }
})();
