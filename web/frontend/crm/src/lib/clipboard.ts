/** Копирование в буфер, работающее и без HTTPS.
 *
 * `navigator.clipboard` живёт только в защищённом контексте. Сайт, открытый по
 * HTTP на адресе вроде 10.0.0.130, к защищённым не относится: там объекта нет
 * вовсе, и обращение к нему падает синхронным TypeError — ещё до промиса, так
 * что `.catch()` на нём бесполезен. Именно поэтому кнопка «Скопировать ссылку»
 * молча показывала адрес вместо копирования.
 *
 * `document.execCommand("copy")` объявлен устаревшим, но это единственный
 * способ скопировать текст на сайте без сертификата, и он работает во всех
 * актуальных браузерах. Держим его запасным путём, а не заменой.
 */
export async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // контекст защищённый, но в доступе отказано (нет разрешения, не тот фокус) —
    // не сдаёмся, пробуем запасной путь ниже
  }
  return legacyCopy(text);
}

function legacyCopy(text: string): boolean {
  const area = document.createElement("textarea");
  area.value = text;
  // readonly — чтобы на мобильных не всплыла клавиатура;
  // элемент должен быть в документе и не скрыт display:none, иначе выделять нечего
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.top = "-1000px";
  area.style.opacity = "0";
  document.body.appendChild(area);
  try {
    area.select();
    // Safari на iOS игнорирует select() у textarea — диапазон приходится задавать руками
    area.setSelectionRange(0, text.length);
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    area.remove();
  }
}
