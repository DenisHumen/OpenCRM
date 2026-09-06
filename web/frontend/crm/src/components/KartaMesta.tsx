import { useEffect, useRef } from "react";

import { Icon } from "./Icon";
import { useApp } from "../lib/app";
import { ssylka_na_kartu } from "../lib/adres";
import { narisovat_minikartu, type CvetaKarty } from "../lib/minikarta";

/** Сколько градусов широты видно по высоте: страна с соседями, а не улица.
 *
 *  Вшитые очертания — уровня страны (Natural Earth 1:110m), и на квартале от
 *  них не осталось бы ничего. Улицу показывает уже гугл, по нажатию. */
const OHVAT = 22;

function cveta_karty(): CvetaKarty {
  const stil = getComputedStyle(document.documentElement);
  const vzyat = (imya: string) => stil.getPropertyValue(imya).trim();
  return {
    more: vzyat("--karta-more"),
    susha: vzyat("--karta-susha"),
    bereg: vzyat("--karta-bereg"),
    setka: vzyat("--karta-setka"),
    tochka: vzyat("--brand"),
  };
}

/**
 * Миниатюра с точкой клиента; нажатие открывает её в гугловых картах.
 *
 * Без координат миниатюры не бывает вовсе — серый прямоугольник на её месте
 * читался бы как «карта не загрузилась».
 */
export function KartaMesta({ lat, lon, podpis }: { lat: number; lon: number; podpis: string }) {
  const { t } = useApp();
  const holst = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    const canvas = holst.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const risovat = () => {
      const dpr = Math.min(2, window.devicePixelRatio || 1);
      const w = canvas.clientWidth;
      const h = canvas.clientHeight;
      if (!w || !h) return;
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      narisovat_minikartu(ctx, {
        lat,
        lon,
        shirina: w,
        vysota: h,
        ohvat: OHVAT,
        cveta: cveta_karty(),
      });
    };
    risovat();

    const razmer = new ResizeObserver(risovat);
    razmer.observe(canvas);
    // Цвета взяты числами в миг отрисовки, и сам по себе холст смену темы не
    // переживает: тёмная карта осталась бы на светлой карточке.
    const tema = new MutationObserver(risovat);
    tema.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    return () => {
      razmer.disconnect();
      tema.disconnect();
    };
  }, [lat, lon]);

  return (
    <a
      className="karta-mini"
      href={ssylka_na_kartu(lat, lon)}
      target="_blank"
      rel="noreferrer"
      title={`${t("mapOpen")}: ${podpis}`}
      // Видимая надпись целиком входит в доступное имя: голосовое управление
      // ищет по тому, что человек читает (WCAG 2.5.3).
      aria-label={`${t("mapOpen")}: ${podpis}`}
    >
      <canvas ref={holst} className="karta-mini-holst" />
      <span className="karta-mini-podpis">
        <Icon name="external" size={12} />
        {/* Место, а не «открыть в картах»: про «открыть» уже сказал значок, а
            на однотонной суше подпись — единственное, что отвечает «где это». */}
        <span className="truncate">{podpis}</span>
      </span>
    </a>
  );
}
