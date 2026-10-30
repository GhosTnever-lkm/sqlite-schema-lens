PRAGMA foreign_keys = ON;
CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE books (id INTEGER PRIMARY KEY, author_id INTEGER NOT NULL REFERENCES authors(id), title TEXT NOT NULL);
CREATE INDEX books_author_idx ON books(author_id);
CREATE VIEW book_titles AS SELECT title FROM books;
