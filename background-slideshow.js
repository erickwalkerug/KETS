/* KETS background slideshow — reliable six-photo rotation. */
(function () {
  'use strict';

  var slides = [
    '/eric-investments-background.png?v=20261028slide1',
    '/executive-car-background.jpg?v=20261028slide2',
    '/executive-boardroom-background.jpg?v=20261028slide3',
    '/eric-investments-gold-background.jpg?v=20261028slide4',
    '/eric-investments-diamonds-background.jpg?v=20261028slide5',
    '/eric-investments-shares-presentation-background.jpg?v=20261028slide6'
  ];
  var availableSlides = [];
  var current = 0;
  var timer = null;

  function applySlide(index) {
    if (!document.documentElement || !availableSlides.length) return;
    current = ((index % availableSlides.length) + availableSlides.length) % availableSlides.length;
    document.documentElement.style.setProperty(
      '--kets-slide-image',
      'url("' + availableSlides[current] + '")',
      'important'
    );
    document.documentElement.setAttribute('data-kets-slide', String(current + 1));
  }

  function startRotation() {
    if (!availableSlides.length) return;
    applySlide(0);
    if (timer) window.clearInterval(timer);
    timer = window.setInterval(function () {
      applySlide(current + 1);
    }, 7000);
    window.addEventListener('pageshow', function () { applySlide(current); });
    document.addEventListener('visibilitychange', function () {
      if (!document.hidden) applySlide(current);
    });
  }

  function start() {
    Promise.all(slides.map(function (src) {
      return new Promise(function (resolve) {
        var image = new Image();
        image.onload = function () { resolve(src); };
        image.onerror = function () { resolve(null); };
        image.src = src;
      });
    })).then(function (results) {
      availableSlides = results.filter(Boolean);
      startRotation();
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start, { once: true });
  } else {
    start();
  }
})();
