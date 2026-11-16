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
  // Причина отказа — для этапа-отказа, сумма и дата оплаты — для этапа-успеха
  document.querySelectorAll('select[data-lost-target]').forEach(function (sel) {
    var lostBox = document.getElementById(sel.dataset.lostTarget);
    var wonBox = document.getElementById(sel.dataset.wonTarget || '');
    function sync() {
      var opt = sel.options[sel.selectedIndex];
      // Текущий этап сделки ничего не требует: поля нужны только при переходе.
      var kind = opt && sel.value !== (sel.dataset.current || '') ? (opt.dataset.kind || '') : '';
      if (lostBox) { lostBox.classList.toggle('hidden', kind !== 'lost'); }
      if (wonBox) { wonBox.classList.toggle('hidden', kind !== 'won'); }
    }
    sel.addEventListener('change', sync); sync();
  });

  // Фильтры, которые применяются сразу при выборе
  document.querySelectorAll('[data-autosubmit]').forEach(function (el) {
    el.addEventListener('change', function () { el.form.submit(); });
  });

  // Подтверждения
  document.querySelectorAll('[data-confirm]').forEach(function (el) {
    el.addEventListener('click', function (e) { if (!window.confirm(el.dataset.confirm)) e.preventDefault(); });
  });
});
