// Dark Visuals — mobile menu, reveal on scroll, hero entrance

document.addEventListener('DOMContentLoaded', function () {
  if (window.lucide) lucide.createIcons();

  // ---- mobile menu ----
  var menuBtn = document.getElementById('menuBtn');
  var mobileMenu = document.getElementById('mobileMenu');
  var iconOpen = document.getElementById('menuIconOpen');
  var iconClose = document.getElementById('menuIconClose');

  if (menuBtn && mobileMenu) {
    menuBtn.addEventListener('click', function () {
      var isHidden = mobileMenu.classList.contains('hidden');
      if (isHidden) {
        mobileMenu.classList.remove('hidden');
        mobileMenu.classList.add('flex');
        if (iconOpen) iconOpen.classList.add('hidden');
        if (iconClose) iconClose.classList.remove('hidden');
      } else {
        mobileMenu.classList.add('hidden');
        mobileMenu.classList.remove('flex');
        if (iconOpen) iconOpen.classList.remove('hidden');
        if (iconClose) iconClose.classList.add('hidden');
      }
    });

    mobileMenu.querySelectorAll('a, button').forEach(function (el) {
      el.addEventListener('click', function () {
        mobileMenu.classList.add('hidden');
        mobileMenu.classList.remove('flex');
        if (iconOpen) iconOpen.classList.remove('hidden');
        if (iconClose) iconClose.classList.add('hidden');
      });
    });
  }

  // ---- reveal on scroll ----
  var reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var revealEls = document.querySelectorAll('.reveal, .reveal-scale');

  if ('IntersectionObserver' in window && !reduceMotion) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add('in');
          io.unobserve(entry.target);
        }
      });
    }, { threshold: 0.12 });
    revealEls.forEach(function (el) { io.observe(el); });
  } else {
    revealEls.forEach(function (el) { el.classList.add('in'); });
  }

  // ---- hero entrance ----
  var heroLeft = document.querySelector('.hero-fade-left');
  var heroScale = document.querySelector('.hero-fade-scale');
  requestAnimationFrame(function () {
    if (heroLeft) heroLeft.classList.add('in');
    if (heroScale) heroScale.classList.add('in');
  });

  // ---- auto-hide flash messages ----
  document.querySelectorAll('.flash').forEach(function (el, i) {
    setTimeout(function () {
      el.style.opacity = '0';
      el.style.transform = 'translateX(20px)';
      setTimeout(function () { el.remove(); }, 300);
    }, 5000 + i * 300);
  });

  if (reduceMotion) return;

  // ---- subtle 3D tilt on cards ----
  var maxTilt = 6;
  document.querySelectorAll('.tilt-card').forEach(function (card) {
    card.addEventListener('mousemove', function (e) {
      var rect = card.getBoundingClientRect();
      var px = (e.clientX - rect.left) / rect.width - 0.5;
      var py = (e.clientY - rect.top) / rect.height - 0.5;
      var rx = (-py * maxTilt).toFixed(2);
      var ry = (px * maxTilt).toFixed(2);
      card.style.transform = 'perspective(1200px) rotateX(' + rx + 'deg) rotateY(' + ry + 'deg) translateZ(4px)';
    });
    card.addEventListener('mouseleave', function () {
      card.style.transform = '';
    });
  });
});
