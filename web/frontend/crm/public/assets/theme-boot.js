/* Тема до первой отрисовки.
 *
 * Ставит на <html> атрибут data-theme раньше, чем браузер что-либо покажет.
 * Без этого при каждом открытии CRM человек со светлой темой видит вспышку
 * тёмного: страница успевает отрисоваться на значениях :root, а бандл приложения
 * доезжает и выполняется позже.
 *
 * Почему отдельным файлом, а не строчкой в index.html и не в main.tsx:
 *   - инлайновый <script> запрещён политикой CSP приложения — script-src 'self'
 *     без 'unsafe-inline' (web/middleware.py, CSP_APP);
 *   - <script type="module"> — это defer: он выполняется ПОСЛЕ разбора
 *     документа, то есть уже после первого кадра. Обычный <script src> в <head>
 *     блокирует отрисовку и выполняется до неё — ровно то, что нужно.
 *
 * Лежит в public/assets/, а не в public/: всё, что не /api, /assets, /media,
 * /static и т.п., перехватывает catch-all SPA в web/main.py и отвечает
 * index.html — скрипт приехал бы как HTML и не выполнился бы вовсе.
 *
 * Файл раздаётся с Cache-Control: immutable, а хэша в имени у него нет —
 * поэтому в index.html он подключён с ?v=N. Правишь файл — увеличивай N,
 * иначе браузеры будут держать старую копию год.
 *
 * Ключ и значения обязаны совпадать с src/lib/theme.ts; сходство сторожит
 * tests/test_screens.py.
 */
(function () {
  var choice = null;
  try {
    choice = localStorage.getItem("crm:theme");
  } catch (e) {
    /* хранилище закрыто настройками браузера — решает система */
  }
  if (choice !== "light" && choice !== "dark") {
    choice =
      window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches
        ? "dark"
        : "light";
  }
  document.documentElement.setAttribute("data-theme", choice);
})();
