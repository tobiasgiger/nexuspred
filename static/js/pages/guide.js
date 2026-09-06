/* Setup guide: the standalone document embedded in the app. */
import { h, pageHead } from "../ui.js";
import { icon } from "../icons.js";

export default {
  title: "Setup Guide",
  render(root) {
    root.append(
      pageHead("Setup Guide", "From zero to live signal routing: deploy, configure, connect TradingView, test safely, go live.", [
        h("a", { class: "btn", href: "/guide", target: "_blank", rel: "noopener" }, icon("external"), "Open standalone"),
      ]),
      h("iframe", { class: "guide-frame", src: "/guide", title: "Fluxbridge setup guide", loading: "lazy" }),
    );
    return () => {};
  },
};
