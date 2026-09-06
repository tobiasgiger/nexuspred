/* Route table: path pattern → page module. */
import overview from "./overview.js";
import discord from "./discord.js";
import logs from "./logs.js";
import journal from "./journal.js";
import webhooks from "./webhooks.js";
import marketplace from "./marketplace.js";
import tools from "./tools.js";
import simulator from "./simulator.js";
import guide from "./guide.js";
import accounts from "./accounts.js";
import discordSettings from "./discordSettings.js";
import users from "./users.js";
import { general, security, updates, alerts, symbols, account } from "./settings.js";

export const ROUTES = [
  { path: "/", page: overview },
  { path: "/discord", page: discord },
  { path: "/logs", page: logs },
  { path: "/journal", page: journal },
  { path: "/webhooks", page: webhooks },
  { path: "/webhooks/:id", page: webhooks },
  { path: "/marketplace", page: marketplace },
  { path: "/settings", redirect: "/settings/general" },
  { path: "/settings/general", page: general },
  { path: "/settings/accounts", page: accounts },
  { path: "/settings/symbols", page: symbols },
  { path: "/settings/discord", page: discordSettings },
  { path: "/settings/alerts", page: alerts },
  { path: "/settings/security", page: security },
  { path: "/settings/account", page: account },
  { path: "/settings/users", page: users },
  { path: "/settings/updates", page: updates },
  { path: "/tools", page: tools },
  { path: "/simulator", page: simulator },
  { path: "/guide", page: guide },
];
