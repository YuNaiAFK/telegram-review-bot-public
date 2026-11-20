import { DatabaseSync } from 'node:sqlite';
import { fileURLToPath } from 'node:url';

export const migrationTables = [
  'requests', 'review_cards', 'auto_unpin_groups', 'pin_provenance',
];

export function readSnapshot(path = fileURLToPath(new URL('../reviews.sqlite3', import.meta.url))) {
  const db = new DatabaseSync(path, { readOnly: true });
  try {
    db.exec('BEGIN');
    if (db.prepare('PRAGMA quick_check').get().quick_check !== 'ok') {
      throw new Error('审核数据库未通过一致性检查');
    }
    const existing = new Set(db.prepare("SELECT name FROM sqlite_master WHERE type='table'").all().map(row => row.name));
    const tables = {};
    for (const name of migrationTables) {
      if (!existing.has(name)) throw new Error(`缺少迁移表：${name}`);
      tables[name] = db.prepare(`SELECT * FROM ${name} ORDER BY rowid`).all();
    }
    const ids = new Set(tables.requests.map(row => row.id));
    if (tables.review_cards.some(card => !ids.has(card.request))) {
      throw new Error('存在无法关联申请的管理员审核卡片');
    }
    const links = tables.requests.map(row => row.invite).filter(Boolean);
    if (new Set(links).size !== links.length) throw new Error('发现重复邀请链接');
    return { version: 1, tables };
  } finally {
    db.close();
  }
}
