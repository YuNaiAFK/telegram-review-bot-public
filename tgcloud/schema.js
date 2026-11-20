import {table, integer, text} from 'sdk/db';
export const t = table('join_reviews', {
  id: integer('id').primaryKey(), chat: integer('chat').notNull(),
  uid: integer('uid').notNull(), dm: integer('dm').notNull(),
  title: text('title').notNull(), question: text('question').notNull(),
  state: text('state').notNull(), answer: text('answer'),
});
