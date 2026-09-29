import { Component, type ReactNode } from "react";

import { useApp } from "../lib/app";

type Svoystva = { children: ReactNode; tekst: string; knopka: string };

/**
 * Сбой отрисовки раздела не гасит всё приложение.
 *
 * Перехватчика не было вовсе: одно исключение в отрисовке снимало меню, панель и
 * несохранённые формы — белый экран. Живой повод — встроенный переводчик Chrome:
 * он подменяет текстовые узлы, и React падает на `removeChild` (разбор 29.09.2026).
 * Ключ по адресу снаружи сбрасывает сбой при переходе в другой раздел.
 */
class Perehvatchik extends Component<Svoystva, { beda: Error | null }> {
  state = { beda: null as Error | null };

  static getDerivedStateFromError(beda: Error) {
    return { beda };
  }

  componentDidCatch(beda: Error) {
    console.error("сбой отрисовки раздела", beda);
  }

  render() {
    if (!this.state.beda) return this.props.children;
    return (
      <div className="card card-pad" role="alert" style={{ margin: 24 }}>
        <p>{this.props.tekst}</p>
        <button type="button" className="btn btn-primary" onClick={() => window.location.reload()}>
          {this.props.knopka}
        </button>
      </div>
    );
  }
}

export function OshibkaOtrisovki({ children }: { children: ReactNode }) {
  const { t } = useApp();
  return (
    <Perehvatchik tekst={t("screenCrashed")} knopka={t("retry")}>
      {children}
    </Perehvatchik>
  );
}
