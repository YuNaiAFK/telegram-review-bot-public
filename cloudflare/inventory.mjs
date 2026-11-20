import { DatabaseSync } from 'node:sqlite';
import { fileURLToPath } from 'node:url';

const db = new DatabaseSync(fileURLToPath(new URL('../reviews.sqlite3', import.meta.url)), { readOnly: true });
const tables = ['requests', 'review_cards', 'auto_unpin_groups', 'pin_provenance'];
try {
  db.exec('BEGIN');
  const check = db.prepare('PRAGMA quick_check').get();
  if (check.quick_check !== 'ok') throw Error('审核数据库未通过一致性检查。');
  const existing = new Set(db.prepare("SELECT name FROM sqlite_master WHERE type='table'").all().map(row => row.name));
  const inventory = Object.fromEntries(tables.map(name => [name, existing.has(name)
    ? db.prepare(`SELECT COUNT(*) AS count FROM ${name}`).get().count : null]));
  const requests = db.prepare('SELECT flow, state, COUNT(*) AS count FROM requests GROUP BY flow, state').all();
  const unused = db.prepare("SELECT COUNT(*) AS count FROM requests WHERE state IN ('invited','redeeming') AND invite IS NOT NULL").get().count;
  console.log(JSON.stringify({ inventory, requests, unusedInvites: unused }, null, 2));
} finally {
  db.close();
}
