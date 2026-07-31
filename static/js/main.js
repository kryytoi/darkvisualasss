// Dark Visuals — 3D tilt + scroll reveal

(function () {
  var reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // ---- scroll reveal ----
  var revealEls = document.querySelectorAll('.reveal');
  if ('IntersectionObserver' in window && !reduceMotion) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add('visible');
          io.unobserve(entry.target);
        }
      });
    }, { threshold: 0.12 });
    revealEls.forEach(function (el) { io.observe(el); });
  } else {
    revealEls.forEach(function (el) { el.classList.add('visible'); });
  }

  if (reduceMotion) return;

  // ---- 3D tilt on cards ----
  var maxTilt = 7;
  document.querySelectorAll('.tilt-card').forEach(function (card) {
    var isHeroShot = card.classList.contains('hero-shot');

    card.addEventListener('mousemove', function (e) {
      var rect = card.getBoundingClientRect();
      var px = (e.clientX - rect.left) / rect.width - 0.5;
      var py = (e.clientY - rect.top) / rect.height - 0.5;
      var rx = (-py * maxTilt).toFixed(2);
      var ry = (px * maxTilt).toFixed(2);
      var base = isHeroShot ? 'rotateX(calc(8deg + ' + rx + 'deg))' : 'rotateX(' + rx + 'deg)';
      card.style.transform = 'perspective(1200px) ' + base + ' rotateY(' + ry + 'deg) translateZ(6px)';
    });

    card.addEventListener('mouseleave', function () {
      card.style.transform = '';
    });
  });

  // ---- parallax orbs ----
  var orbs = document.querySelectorAll('.orb');
  var ticking = false;
  window.addEventListener('scroll', function () {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(function () {
      var y = window.scrollY;
      orbs.forEach(function (orb, i) {
        orb.style.marginTop = (y * (0.04 + i * 0.03)) + 'px';
      });
      ticking = false;
    });
  }, { passive: true });
})();
