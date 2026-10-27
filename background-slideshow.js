/* KETS background slideshow - robust CSS override and image preload */
(function () {
  'use strict';
  var slides = [
    '/eric-investments-background.png?v=20261027slide1fix',
    '/executive-car-background.jpg?v=20261027slide2fix',
    '/executive-boardroom-background.jpg?v=20261027slide3fix'
  ];
  var current = 0;
  var style;

  function applySlide(index) {
    current = ((index % slides.length) + slides.length) % slides.length;
    if (!style) return;
    // Override the legacy !important background shorthand with a more specific rule.
    style.textContent = 'html body::before {' +
      'content:"" !important; display:block !important; position:fixed !important;' +
      'inset:0 !important; z-index:-1 !important; pointer-events:none !important;' +
      'background:linear-gradient(135deg,rgba(3,8,18,.16),rgba(3,8,18,.28)), ' +
      'url("' + slides[current] + '") center center / cover no-repeat !important;' +
      '}';
  }

  function start() {
    style = document.getElementById('kets-background-slideshow-style');
    if (!style) {
      style = document.createElement('style');
      style.id = 'kets-background-slideshow-style';
      document.head.appendChild(style);
    }

    // Preload each image; rotating continues even if one asset fails.
    slides.forEach(function (src) {
      var image = new Image();
      image.src = src;
    });

    applySlide(0);
    window.setInterval(function () {
      applySlide(current + 1);
    }, 7000);

    // Re-apply if the page is restored from the mobile browser's back-forward cache.
    window.addEventListener('pageshow', function () { applySlide(current); });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start, { once: true });
  } else {
    start();
  }
})();
