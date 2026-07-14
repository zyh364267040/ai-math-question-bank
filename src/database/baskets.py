"""Transactional operations for the single-user local question basket."""


def _basket_id(connection):
    return connection.execute(
        "SELECT id FROM baskets WHERE basket_key='default'"
    ).fetchone()[0]


def add(connection, question_code):
    with connection:
        question = connection.execute(
            "SELECT id FROM questions WHERE question_code=? AND deleted_at IS NULL", (question_code,)
        ).fetchone()
        if question is None:
            return False
        basket = _basket_id(connection)
        position = connection.execute(
            "SELECT COALESCE(MAX(position),0)+1 FROM basket_items WHERE basket_id=?", (basket,)
        ).fetchone()[0]
        connection.execute(
            "INSERT OR IGNORE INTO basket_items(basket_id,question_id,position) VALUES(?,?,?)",
            (basket, question[0], position),
        )
    return True


def remove(connection, question_code):
    with connection:
        basket = _basket_id(connection)
        connection.execute(
            "DELETE FROM basket_items WHERE basket_id=? AND question_id=(SELECT id FROM questions WHERE question_code=?)",
            (basket, question_code),
        )
        _compact(connection, basket)


def clear(connection):
    with connection:
        connection.execute("DELETE FROM basket_items WHERE basket_id=?", (_basket_id(connection),))


def _compact(connection, basket):
    ids = [row[0] for row in connection.execute(
        "SELECT id FROM basket_items WHERE basket_id=? ORDER BY position,id", (basket,)
    )]
    connection.execute("UPDATE basket_items SET position=position+1000000 WHERE basket_id=?", (basket,))
    connection.executemany("UPDATE basket_items SET position=? WHERE id=?", enumerate(ids, 1))


def move(connection, question_code, direction):
    with connection:
        basket = _basket_id(connection)
        rows = connection.execute(
            """SELECT bi.id,q.question_code FROM basket_items bi JOIN questions q ON q.id=bi.question_id
               WHERE bi.basket_id=? AND q.deleted_at IS NULL ORDER BY bi.position""", (basket,)
        ).fetchall()
        index = next((i for i,row in enumerate(rows) if row[1] == question_code), None)
        if index is None:
            return
        other = index - 1 if direction == "up" else index + 1
        if other < 0 or other >= len(rows):
            return
        connection.execute("UPDATE basket_items SET position=position+1000000 WHERE id IN (?,?)", (rows[index][0], rows[other][0]))
        connection.execute("UPDATE basket_items SET position=? WHERE id=?", (other + 1, rows[index][0]))
        connection.execute("UPDATE basket_items SET position=? WHERE id=?", (index + 1, rows[other][0]))
