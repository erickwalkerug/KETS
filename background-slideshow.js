/* KETS background slideshow: preserves the existing background and cycles through two added photos. */
(function () {
  'use strict';
  var slides = [
    '/eric-investments-background.png?v=20261027slide1',
    '/executive-car-background.jpg?v=20261027slide2',
    '/executive-boardroom-background.jpg?v=20261027slide3'
  ];
  var current = 0;
  function setSlide(index) {
    document.body.style.setProperty(
      '--kets-background-image',
      'url("' + slides[index] + '")',
      'important'
    );
    // Apply to the existing background layer so all existing page transparency and layout remain intact.
    var layer = document.querySelector('body::before');
    current = index;
  }
  function start() {
    document.body.style.setProperty('--kets-background-image', 'url("' + slides[0] + '")');
    var style = document.createElement('style');
    style.id = 'kets-background-slideshow-style';
    style.textContent = [
      'body::before { background-image: var(--kets-background-image) !important; transition: opacity 0.8s ease-in-out; }'
    ].join('');
    document.head.appendChild(style);
    window.setInterval(function () {
      current = (current + 1) % slides.length;
      document.body.style.setProperty('--kets-background-image', 'url("' + slides[current] + '")', 'important');
    }, 7000);
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start, { once: true });
  } else {
    start();
  }
})();
