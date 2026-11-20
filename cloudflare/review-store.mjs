export const dbColumns = {
  requests: ['id','event','chat','uid','dm','label','question','photos','common','answer','state','notified','username','flow','invite','redeem_event'],
  review_cards: ['admin','message','request','stage'],
  auto_unpin_groups: ['chat'],
  pin_provenance: ['chat','message','observed','kind'],
};

const ddl = [
  `CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY, event INTEGER UNIQUE, chat INTEGER, uid INTEGER, dm INTEGER,
    label TEXT, question TEXT, photos INTEGER, common TEXT, answer TEXT,
    state TEXT DEFAULT 'waiting', notified INTEGER DEFAULT 0, username TEXT,
    flow TEXT NOT NULL DEFAULT 'legacy', invite TEXT, redeem_event INTEGER
  )`,
  `CREATE UNIQUE INDEX IF NOT EXISTS request_invite ON requests(invite) WHERE invite IS NOT NULL`,
  `CREATE TABLE IF NOT EXISTS review_cards (
    admin INTEGER NOT NULL, message INTEGER NOT NULL, request INTEGER NOT NULL,
    stage TEXT NOT NULL DEFAULT 'review', PRIMARY KEY(admin,message)
  )`,
  `CREATE TABLE IF NOT EXISTS auto_unpin_groups (chat INTEGER PRIMARY KEY)`,
  `CREATE TABLE IF NOT EXISTS pin_provenance (
    chat INTEGER, message INTEGER, observed INTEGER, kind TEXT, PRIMARY KEY(chat,message)
  )`,
];

export function initializeSchema(sql) {
  for (const statement of ddl) sql.exec(statement);
}

export function importInitialSnapshot(storage, snapshot, onCommit = () => {}) {
  if (snapshot?.version !== 1 || !snapshot.tables) throw Error('不支持的审核快照格式');
  const { sql } = storage;
  const entries = Object.entries(dbColumns);
  const rowsByTable = snapshot.tables;
  for (const [name, columns] of entries) {
    if (!Array.isArray(rowsByTable[name])) throw Error(`缺少表：${name}`);
    for (const row of rowsByTable[name]) {
      if (!row || columns.some(col => !Object.hasOwn(row,col) ||
          !(row[col] === null || typeof row[col] === 'string' || Number.isSafeInteger(row[col])))) {
        throw Error(`数据不符合迁移表结构：${name}`);
      }
    }
  }
  const ids = new Set(rowsByTable.requests.map(row => row.id));
  if (rowsByTable.review_cards.some(row => !ids.has(row.request))) throw Error('有孤立的审核卡片');

  return storage.transactionSync(() => {
    initializeSchema(sql);
    for (const [name] of entries) {
      if (sql.exec(`SELECT COUNT(*) AS n FROM ${name}`).one().n) {
        throw Error('目标数据库已有数据，不能覆盖');
      }
    }
    const counts = {};
    for (const [name, columns] of entries) {
      const binding = columns.map(() => '?').join(',');
      const statement = `INSERT INTO ${name} (${columns.join(',')}) VALUES (${binding})`;
      for (const row of rowsByTable[name]) sql.exec(statement, ...columns.map(col => row[col]));
      counts[name] = rowsByTable[name].length;
    }
    onCommit();
    return counts;
  });
}
