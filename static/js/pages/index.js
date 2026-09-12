/* Route table: path pattern → page module. */
import overview from "./overview.js";
import discord from "./discord.js";
import logs from "./logs.js";
import journal from "./journal.js";
import webhooks from "./webhooks.js";
import marketplace from "./marketplace.js";
import copyTrading from "./copy.js";
import tools from "./tools.js";
import simulator from "./simulator.js";
import guide from "./guide.js";
import accounts from "./accounts.js";
import discordSettings from "./discordSettings.js";
import users from "./users.js";
import agents from "./agents.js";
import news from "./news.js";
import calendar from "./calendar.js";
import automations from "./automations.js";
import subscriptions from "./subscriptions.js";
import { general, security, updates, alerts, symbols, account } from "./settings.js";

export const ROUTES = [
  { path: "/", page: overview },
  { path: "/discord", page: discord },
  { path: "/logs", page: logs },
  { path: "/journal", page: journal },
  { path: "/calendar", page: calendar },
  { path: "/webhooks", page: webhooks },
  { path: "/webhooks/:id", page: webhooks },
  { path: "/marketplace", page: marketplace },
  { path: "/copy", page: copyTrading },
  { path: "/copy/:id", page: copyTrading },
  { path: "/subscriptions", page: subscriptions },
  { path: "/settings", redirect: "/settings/general" },
  { path: "/settings/general", page: general },
  { path: "/settings/accounts", page: accounts },
  { path: "/settings/symbols", page: symbols },
  { path: "/settings/discord", page: discordSettings },
  { path: "/settings/alerts", page: alerts },
  { path: "/settings/automations", page: automations },
  { path: "/settings/security", page: security },
  { path: "/settings/account", page: account },
  { path: "/settings/users", page: users },
  { path: "/settings/agents", page: agents },
  { path: "/settings/news", page: news },
  { path: "/settings/updates", page: updates },
  { path: "/tools", page: tools },
  { path: "/simulator", page: simulator },
  { path: "/guide", page: guide },
];
