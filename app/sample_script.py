"""Example change script shown from the editor menu."""

SAMPLE_SCRIPT = """\
-- 테이블, 인덱스, INSERT/UPDATE/DELETE 는 세미콜론(;)으로 끝냅니다.
-- 프로시저, 함수, 패키지, 트리거, 익명 블록은 마지막에 슬래시(/) 한 줄로 구분합니다.
-- 아래 테이블 생성은 한 번만 실행할 수 있습니다. 다시 실행하려면 테이블 이름을 바꾸세요.

CREATE TABLE demo_change_log (
    id          NUMBER PRIMARY KEY,
    message     VARCHAR2(200),
    created_at  DATE DEFAULT SYSDATE
);

CREATE INDEX ix_demo_change_log_created
    ON demo_change_log (created_at);

INSERT INTO demo_change_log (id, message)
VALUES (1, 'initial');

CREATE OR REPLACE FUNCTION demo_fn(p_id IN NUMBER)
RETURN VARCHAR2
IS
    v_message VARCHAR2(200);
BEGIN
    SELECT message
      INTO v_message
      FROM demo_change_log
     WHERE id = p_id;
    RETURN v_message;
END;
/

CREATE OR REPLACE PROCEDURE demo_proc(
    p_id      IN NUMBER,
    p_message IN VARCHAR2
)
IS
BEGIN
    UPDATE demo_change_log
       SET message = p_message
     WHERE id = p_id;
    DBMS_OUTPUT.PUT_LINE('updated ' || SQL%ROWCOUNT);
END;
/

CREATE OR REPLACE PACKAGE demo_pkg IS
    PROCEDURE touch(p_id IN NUMBER);
END;
/

CREATE OR REPLACE PACKAGE BODY demo_pkg IS
    PROCEDURE touch(p_id IN NUMBER) IS
    BEGIN
        UPDATE demo_change_log
           SET created_at = SYSDATE
         WHERE id = p_id;
    END;
END;
/

UPDATE demo_change_log
   SET message = 'changed'
 WHERE id = 1;

BEGIN
    demo_proc(1, 'from proc');
END;
/
"""
