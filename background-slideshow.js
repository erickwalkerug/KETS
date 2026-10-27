/* KETS background slideshow: rotate the original background and two added photos.
   Uses a real background-image rule (not a custom property) so it overrides the
   existing !important background declaration in styles.css. */
(function () {
  'use strict';

  var slides = [
    '/eric-investments-background.png?v=20261027slide1',
    '/executive-car-background.jpg?v=20261027slide2',
    '/executive-boardroom-background.jpg?v=20261027slide3'
  ];
  var current = 0;
  var style;

  function applySlide(index) {
    current = index % slides.length;
    if (!style) return;
    style.textContent =
      'body::before {' +
      ' background-image: linear-gradient(135deg,rgba(3,8,18,.16),rgba(3,8,18,.28)), url("' +
      slides[current] + '") !important;' +
      ' transition: background-image .8s ease-in-out;' +
      '}';
  }

  function start() {
    if (document.getElementById('kets-background-slideshow-style')) {
      style = document.getElementById('kets-background-slideshow-style');
    } else {
      style = document.createElement('style');
      style.id = 'kets-background-slideshow-style';
      document.head.appendChild(style);
    }

    // Preload the photos so each change is ready when the interval fires.
    slides.forEach(function (src) {
      var image = new Image();
      image.src = src;
    });

    applySlide(0);
    window.setInterval(function () {
      applySlide((current + 1) % slides.length);
    }, 7000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start, { once: true });
  } else {
    start();
  }
})();
