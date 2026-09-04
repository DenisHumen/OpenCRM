// Шрифты со своего домена, а не с fonts.googleapis.com: CSP приложения
// (style-src 'self') внешнюю таблицу стилей всё равно блокировала, и интерфейс
// молча рендерился системными шрифтами. Вариативные — один файл на все веса,
// подсеты по unicode-range, так что кириллица едет только там, где нужна.
import "@fontsource-variable/inter";
import "@fontsource-variable/source-serif-4";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import App from "./App";
import { AppProvider } from "./lib/app";
import { LiveProvider } from "./lib/live";
import "./styles.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <AppProvider>
        <LiveProvider>
          <App />
        </LiveProvider>
      </AppProvider>
    </BrowserRouter>
  </StrictMode>,
);
