import { DatabaseSync } from 'node:sqlite';
import { isDeepStrictEqual } from 'node:util';
import { readSnapshot } from './migration-snapshot.mjs';
import { dbColumns, importInitialSnapshot } from './review-store.mjs';

const source = readSnapshot();
const db = new DatabaseSync(':memory:');
const storage = {
  sql: { exec(statement,...params) {
    const q = db.prepare(statement);
    if (statement.startsWith('SELECT')) {
      return { one: () => q.get(...params) };
    }
    q.run(...params);
  } },
  transactionSync(fn) {
    db.exec('BEGIN');
    try { const result=fn(); db.exec('COMMIT'); return result; }
    catch (e) { db.exec('ROLLBACK'); throw e; }
  },
};
try {
  const counts = importInitialSnapshot(storage, source);
  for (const [name, columns] of Object.entries(dbColumns)) {
    const normalize = (rows) => rows.map(row => columns.map(col => row[col]));
    const migrated = db.prepare(`SELECT * FROM ${name} ORDER BY rowid`).all();
    if (!isDeepStrictEqual(normalize(migrated),normalize(source.tables[name]))) {
      throw Error(`数据对照失败：${name}`);
    }
  }
  let refused = false;
  try { importInitialSnapshot(storage, source); } catch (error) { refused = /不能覆盖/.test(error.message); }
  if (!refused) throw Error('二次导入没有拒绝覆盖');
  console.log(JSON.stringify(counts));
} finally { db.close(); }
