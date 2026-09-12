/* Setup guide: the standalone document embedded in the app. */
import { h, pageHead } from "../ui.js";
import { icon } from "../icons.js";
import { t } from "../i18n.js";

export default {
  title: t("Setup Guide"),
  render(root) {
    root.append(
      pageHead(t("Setup Guide"), t("From zero to live signal routing: deploy, configure, connect TradingView, test safely, go live."), [
        h("a", { class: "btn", href: "/guide", target: "_blank", rel: "noopener" }, icon("external"), t("Open standalone")),
      ]),
      h("iframe", { class: "guide-frame", src: "/guide", title: t("Fluxbridge setup guide"), loading: "lazy" }),
    );
    return () => {};
  },
};
