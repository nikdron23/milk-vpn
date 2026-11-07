document.addEventListener('click', function (e) {
  var b = e.target.closest('[data-copy]');
  if (!b || !navigator.clipboard) return;
  navigator.clipboard.writeText(b.dataset.copy).then(function () {
    var t = b.textContent;
    b.textContent = 'Скопировано ✓';
    setTimeout(function () { b.textContent = t; }, 1600);
  });
});
