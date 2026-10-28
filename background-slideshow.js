/* KETS background slideshow — six-photo version; preserve the existing app and behavior. */
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
  var current = -1;
  var timer = null;

  function applySlide(index) {
    if (!document.documentElement || !slides.length) return;
    current = ((index % slides.length) + slides.length) % slides.length;
    document.documentElement.style.setProperty(
      '--kets-slide-image',
      'url("' + slides[current] + '")',
      'important'
    );
    document.documentElement.setAttribute('data-kets-slide', String(current + 1));
  }

  function start() {
    if (timer) window.clearInterval(timer);
    slides.forEach(function (src) {
      var image = new Image();
      image.onload = function () {};
      image.src = src;
    });
    applySlide(0);
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
