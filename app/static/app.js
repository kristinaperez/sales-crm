// Небольшие удобства интерфейса. Вся логика — на сервере.
document.addEventListener('DOMContentLoaded', function () {
  // Переключение способа импорта
  var modes = document.querySelectorAll('input[name=mode]');
  function showMode() {
    var current = (document.querySelector('input[name=mode]:checked') || {}).value;
    document.querySelectorAll('[data-mode]').forEach(function (el) {
      el.classList.toggle('hidden', el.dataset.mode.split(' ').indexOf(current) === -1);
    });
    document.querySelectorAll('.tabs label[data-for]').forEach(function (el) {
      el.classList.toggle('on', el.dataset.for === current);
    });
  }
  modes.forEach(function (m) { m.addEventListener('change', showMode); });
  if (modes.length) showMode();

  // «Выбрать все» в таблицах
  document.querySelectorAll('[data-check-all]').forEach(function (master) {
    master.addEventListener('change', function () {
      document.querySelectorAll('input[name="' + master.dataset.checkAll + '"]').forEach(function (cb) { cb.checked = master.checked; });
    });
  });

  // Причина отказа показывается только для этапа-отказа
  document.querySelectorAll('select[data-lost-target]').forEach(function (sel) {
    var target = document.getElementById(sel.dataset.lostTarget);
    function sync() {
      var opt = sel.options[sel.selectedIndex];
      var lost = opt && opt.dataset.kind === 'lost';
      if (target) { target.classList.toggle('hidden', !lost); }
    }
    sel.addEventListener('change', sync); sync();
  });

  // Подтверждения
  document.querySelectorAll('[data-confirm]').forEach(function (el) {
    el.addEventListener('click', function (e) { if (!window.confirm(el.dataset.confirm)) e.preventDefault(); });
  });
});
