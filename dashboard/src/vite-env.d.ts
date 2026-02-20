/// <reference types="vite/client" />

declare const Plotly: {
  newPlot(
    el: HTMLElement,
    data: Plotly.Data[],
    layout?: Partial<Plotly.Layout>,
    config?: Partial<Plotly.Config>
  ): void;
  react(
    el: HTMLElement,
    data: Plotly.Data[],
    layout?: Partial<Plotly.Layout>,
    config?: Partial<Plotly.Config>
  ): void;
  purge(el: HTMLElement): void;
};

declare namespace Plotly {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  type Data = any;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  type Layout = any;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  type Config = any;
}
