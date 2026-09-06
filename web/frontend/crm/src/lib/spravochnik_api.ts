// Порождено скриптом scripts/spravochnik_api.py из раздела «API сайта магазина» docs/osnovy/04-api.md.
// Руками не править: правится справочник, потом запускается скрипт.

export type VidDostupa = "otkryto" | "sotrudnik" | "pravo" | "klyuch" | "inoe";

export type Ruchka = {
  metod: string;
  put: string;
  vid: VidDostupa;
  dostup: string;
  opisanie: string;
  podrazdel: string;
  vne_api: boolean;
};

export type RazdelApi = { nazvanie: string; ruchki: Ruchka[] };

export const SPRAVOCHNIK_API: RazdelApi[] = [
  {
    "nazvanie": "Чтение",
    "ruchki": [
      {
        "metod": "GET",
        "put": "/site/catalog",
        "vid": "klyuch",
        "dostup": "catalog.read",
        "opisanie": "Страница карточек, `?page=&per_page=` (до 200). Виден товар, по складу ключа было хоть одно движение, и все услуги; ключ без склада — одни услуги",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/site/catalog/{product_id}",
        "vid": "klyuch",
        "dostup": "catalog.read",
        "opisanie": "Одна карточка; `404 product_not_found` — не опубликован или удалён",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/site/changes",
        "vid": "klyuch",
        "dostup": "catalog.read",
        "opisanie": "Лента изменений: `?since=<курсор>&limit=<до 200>`. Без `since` — полная выгрузка",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/site/stock",
        "vid": "klyuch",
        "dostup": "stock.read",
        "opisanie": "Наличие: `?id=17,42` либо `?sku=A,B`, до 200 позиций (`422 too_many_ids`)",
        "podrazdel": "",
        "vne_api": false
      }
    ]
  },
  {
    "nazvanie": "Запись",
    "ruchki": [
      {
        "metod": "POST",
        "put": "/site/orders",
        "vid": "klyuch",
        "dostup": "orders.write",
        "opisanie": "Заказ со сроком брони. `201` — новый, `200` — повтор с тем же `site_ref` (тот же заказ, второй не заводится)",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "GET",
        "put": "/site/orders/{site_ref}",
        "vid": "klyuch",
        "dostup": "orders.read",
        "opisanie": "Свой заказ по чужому номеру; чужой или несуществующий — `404 order_not_found`",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/site/orders/{site_ref}/cancel",
        "vid": "klyuch",
        "dostup": "orders.write",
        "opisanie": "Снять бронь: заказ уходит в `cancelled`, товар свободен. После накладной — `409 order_already_fulfilled`",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/site/customers",
        "vid": "klyuch",
        "dostup": "customers.write",
        "opisanie": "Завести карточку клиента или узнать свою; ответ всегда одной формы, `202`",
        "podrazdel": "",
        "vne_api": false
      },
      {
        "metod": "POST",
        "put": "/site/leads",
        "vid": "klyuch",
        "dostup": "leads.write",
        "opisanie": "Та же заявка, что `/public/leads`, только по ключу сайта; `202 {\"status\": \"accepted\"}`",
        "podrazdel": "",
        "vne_api": false
      }
    ]
  },
  {
    "nazvanie": "Снимок товара",
    "ruchki": [
      {
        "metod": "GET",
        "put": "/media/product/{filename}",
        "vid": "inoe",
        "dostup": "uid 128 бит; блок `warehouse` включён; товар опубликован",
        "opisanie": "`<uid>.webp` и `<uid>-thumb.webp` из карточки; `Cache-Control: public, max-age=31536000, immutable`, `X-Content-Type-Options: nosniff`. Неопубликованный товар — `404`, а не «есть, но не покажем»",
        "podrazdel": "",
        "vne_api": true
      }
    ]
  }
];
