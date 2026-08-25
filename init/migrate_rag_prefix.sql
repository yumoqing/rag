-- rag 模块表名加 rag_ 前缀（模块跨应用复用，避免与业务表冲突）
-- 幂等：仅当旧表存在且新表不存在时才 RENAME。
-- 用法: mysql -h <host> -u<user> -p<pwd> <db> < migrate_rag_prefix.sql

DELIMITER $$
DROP PROCEDURE IF EXISTS rag_rename_tables$$
CREATE PROCEDURE rag_rename_tables()
BEGIN
    DECLARE done INT DEFAULT 0;
    DECLARE oldname VARCHAR(64);
    DECLARE cur CURSOR FOR
        SELECT t FROM (
            SELECT 'knowledge_bases' AS t UNION ALL
            SELECT 'documents'            UNION ALL
            SELECT 'document_chunks'      UNION ALL
            SELECT 'tags'                 UNION ALL
            SELECT 'media_tags'           UNION ALL
            SELECT 'entities'             UNION ALL
            SELECT 'entity_relations'     UNION ALL
            SELECT 'engine_configs'       UNION ALL
            SELECT 'subscriptions'        UNION ALL
            SELECT 'org_storage_limits'   UNION ALL
            SELECT 'usage_logs'
        ) x;
    DECLARE CONTINUE HANDLER FOR NOT FOUND SET done = 1;

    OPEN cur;
    read_loop: LOOP
        FETCH cur INTO oldname;
        IF done THEN LEAVE read_loop; END IF;

        SET @has_old = (SELECT COUNT(*) FROM information_schema.TABLES
                        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = oldname);
        SET @has_new = (SELECT COUNT(*) FROM information_schema.TABLES
                        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = CONCAT('rag_', oldname));

        IF @has_old = 1 AND @has_new = 0 THEN
            SET @sql = CONCAT('RENAME TABLE `', oldname, '` TO `rag_', oldname, '`');
            PREPARE st FROM @sql;
            EXECUTE st;
            DEALLOCATE PREPARE st;
            SELECT CONCAT('RENAMED: ', oldname, ' -> rag_', oldname) AS result;
        ELSEIF @has_new = 1 THEN
            SELECT CONCAT('SKIP (already migrated): rag_', oldname) AS result;
        ELSE
            SELECT CONCAT('SKIP (no such table): ', oldname) AS result;
        END IF;
    END LOOP;
    CLOSE cur;
END$$
DELIMITER ;

CALL rag_rename_tables();
DROP PROCEDURE rag_rename_tables;
