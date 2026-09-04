-- rag 模块 uapi 外部服务配置种子（幂等：INSERT IGNORE）
-- 覆盖 upapp(rag-*) + uapi(rag-*) + uapiio，供任何宿主应用(ragserver/pipeline-app)导入

/*M!999999\- enable the sandbox mode */ 
/*!40103 SET @OLD_TIME_ZONE=@@TIME_ZONE */;
/*!40103 SET TIME_ZONE='+00:00' */;
/*!40014 SET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0 */;
/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */;
/*!40111 SET @OLD_SQL_NOTES=@@SQL_NOTES, SQL_NOTES=0 */;

INSERT IGNORE INTO `upapp` (`id`, `name`, `description`, `ownerid`, `auth_apiname`, `secretkey`, `baseurl`, `myappid`, `dynamic_func`) VALUES ('rag-embedding','CLIP向量化','CLIP模型向量化服务',NULL,NULL,NULL,'https://embedding.opencomputing.net:10443','',NULL),('rag-face','人脸服务','人脸检测/识别/比对',NULL,NULL,NULL,'https://face.opencomputing.net:10443','',NULL),('rag-graph','图数据库','Neo4j图数据库',NULL,NULL,NULL,'https://graphdb.opencomputing.net:10443','',NULL),('rag-ner','NER实体识别','命名实体识别服务',NULL,NULL,NULL,'https://entities.opencomputing.net:10443','',NULL),('rag-reranker','Reranker重排','BGE Reranker重排序服务',NULL,NULL,NULL,'https://reranker.opencomputing.net:10443','',NULL),('rag-vdb','VDB向量库','Milvus向量数据库',NULL,NULL,NULL,'https://vectordb.opencomputing.net:10443','',NULL),('rag-voiceprint','声纹服务','0','0',NULL,NULL,'https://media.opencomputing.net',NULL,NULL);
/*!40103 SET TIME_ZONE=@OLD_TIME_ZONE */;

/*!40101 SET SQL_MODE=@OLD_SQL_MODE */;
/*!40014 SET FOREIGN_KEY_CHECKS=@OLD_FOREIGN_KEY_CHECKS */;
/*!40111 SET SQL_NOTES=@OLD_SQL_NOTES */;

/*M!999999\- enable the sandbox mode */ 
/*!40103 SET @OLD_TIME_ZONE=@@TIME_ZONE */;
/*!40103 SET TIME_ZONE='+00:00' */;
/*!40014 SET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0 */;
/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */;
/*!40111 SET @OLD_SQL_NOTES=@@SQL_NOTES, SQL_NOTES=0 */;

INSERT IGNORE INTO `uapi` (`id`, `name`, `title`, `description`, `need_auth`, `stream`, `path`, `httpmethod`, `chunk_match`, `headers`, `params`, `data`, `response`, `ioid`, `callbackurl`, `upappid`) VALUES ('emb-embed','embed','文本向量化',NULL,'0',NULL,'/api/embed','POST',NULL,'{\n    \"Content-Type\": \"application/json\"\n}',NULL,'{\"texts\": {{json.dumps(texts)}}, \"images\": {{json.dumps(images)}}}',NULL,'io-embedding',NULL,'rag-embedding'),('face-compare','compare','人脸比对',NULL,'0',NULL,'/api/compare','POST',NULL,'{\n    \"Content-Type\": \"application/json\"\n}',NULL,'{\"image1\": \"{{image1}}\", \"image2\": \"{{image2}}\"}',NULL,'io-face',NULL,'rag-face'),('face-detect','detect','人脸检测',NULL,'0',NULL,'/api/detect','POST',NULL,'{\n    \"Content-Type\": \"application/json\"\n}',NULL,'{\"images\": {{json.dumps(images)}}}',NULL,'io-face',NULL,'rag-face'),('face-recognize','recognize','人脸识别',NULL,'0',NULL,'/api/recognize','POST',NULL,'{\n    \"Content-Type\": \"application/json\"\n}',NULL,'{\"images\": {{json.dumps(images)}}}',NULL,'io-face',NULL,'rag-face'),('graph-add-edge','add_edge','添加边',NULL,'0',NULL,'/api/graph/add_edge','POST',NULL,'{\n    \"Content-Type\": \"application/json\"\n}',NULL,'{\"graph\": \"{{graph}}\", \"source\": \"{{source}}\", \"target\": \"{{target}}\", \"attrs\": {{json.dumps(attrs)}}}',NULL,'io-graph',NULL,'rag-graph'),('graph-add-node','add_node','添加节点',NULL,'0',NULL,'/api/graph/add_node','POST',NULL,'{\n    \"Content-Type\": \"application/json\"\n}',NULL,'{\"graph\": \"{{graph}}\", \"node_id\": \"{{node_id}}\", \"attrs\": {{json.dumps(attrs)}}}',NULL,'io-graph',NULL,'rag-graph'),('graph-delete','delete','删除图',NULL,'0',NULL,'/api/graph/delete','POST',NULL,'{\n    \"Content-Type\": \"application/json\"\n}',NULL,'{\"graph\": \"{{graph}}\"}',NULL,'io-graph',NULL,'rag-graph'),('graph-neighbors','neighbors','邻居查询',NULL,'0',NULL,'/api/graph/neighbors','GET',NULL,'{\n    \"Content-Type\": \"application/json\"\n}',NULL,'{\"graph\": \"{{graph}}\", \"node_id\": \"{{node_id}}\"}',NULL,'io-graph',NULL,'rag-graph'),('graph-query','query','查询图',NULL,'0',NULL,'/api/graph/query','POST',NULL,'{\n    \"Content-Type\": \"application/json\"\n}',NULL,'{\"graph\": \"{{graph}}\", \"query\": \"{{query}}\"}',NULL,'io-graph',NULL,'rag-graph'),('graph-save','save','保存图',NULL,'0',NULL,'/api/graph/save','POST',NULL,'{\n    \"Content-Type\": \"application/json\"\n}',NULL,'{\"graph\": \"{{graph}}\", \"data\": {{json.dumps(data)}}}',NULL,'io-graph',NULL,'rag-graph'),('ner-entities','entities','实体识别',NULL,'0',NULL,'/api/extract','POST',NULL,'{\n    \"Content-Type\": \"application/json\"\n}',NULL,'{\"text\": \"{{text}}\", \"entities\": {{json.dumps(entities)}}, \"threshold\": {{threshold}}}',NULL,'io-ner',NULL,'rag-ner'),('reranker-rerank','rerank','文本重排序',NULL,'0',NULL,'/api/rerank','POST',NULL,'{\n    \"Content-Type\": \"application/json\"\n}',NULL,'{\"query\": \"{{query}}\", \"documents\": {{json.dumps(documents)}}, \"top_k\": {{top_k}}}',NULL,'io-reranker',NULL,'rag-reranker'),('vdb-delete','delete','向量删除',NULL,'0',NULL,'/v1/delete','POST',NULL,'{\n    \"Content-Type\": \"application/json\"\n}',NULL,'{\"colname\": \"{{colname}}\", \"pks\": {{json.dumps(ids)}}}',NULL,'io-vdb-vector',NULL,'rag-vdb'),('vdb-search','search','向量检索',NULL,'0',NULL,'/v1/query','POST',NULL,'{\n    \"Content-Type\": \"application/json\"\n}',NULL,'{\"colname\": \"{{colname}}\", \"vector\": {{json.dumps(vector)}}, \"pagerows\": {{top_k}}, \"output_fields\": [\"text\"]}',NULL,'io-vdb-vector',NULL,'rag-vdb'),('vdb-upsert','upsert','向量插入',NULL,'0',NULL,'/v1/upsert','POST',NULL,'{\n    \"Content-Type\": \"application/json\"\n}',NULL,'{\"colname\": \"{{colname}}\", \"data\": {{json.dumps(data)}}}',NULL,'io-vdb-vector',NULL,'rag-vdb'),('voiceprint-extract','voiceprint-extract','声纹提取',NULL,'0',NULL,'/voiceprint/extract/submit','POST',NULL,NULL,NULL,NULL,NULL,NULL,NULL,'rag-voiceprint'),('voiceprint-status','voiceprint-status','声纹状态查询',NULL,'0',NULL,'/voiceprint/extract/status','GET',NULL,NULL,NULL,NULL,NULL,NULL,NULL,'rag-voiceprint');
/*!40103 SET TIME_ZONE=@OLD_TIME_ZONE */;

/*!40101 SET SQL_MODE=@OLD_SQL_MODE */;
/*!40014 SET FOREIGN_KEY_CHECKS=@OLD_FOREIGN_KEY_CHECKS */;
/*!40111 SET SQL_NOTES=@OLD_SQL_NOTES */;

/*M!999999\- enable the sandbox mode */ 
/*!40103 SET @OLD_TIME_ZONE=@@TIME_ZONE */;
/*!40103 SET TIME_ZONE='+00:00' */;
/*!40014 SET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0 */;
/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */;
/*!40111 SET @OLD_SQL_NOTES=@@SQL_NOTES, SQL_NOTES=0 */;

INSERT IGNORE INTO `uapiio` (`id`, `name`, `description`, `input_fields`) VALUES ('io-embedding','CLIP向量化','文本/图片向量化','[{\"name\":\"texts\",\"label\":\"文本列表\",\"uitype\":\"json\",\"required\":true},{\"name\":\"model\",\"label\":\"模型名\",\"uitype\":\"str\"}]'),('io-face','人脸服务','人脸检测/识别/比对','[{\"name\":\"image_url\",\"label\":\"图片URL\",\"uitype\":\"str\",\"required\":true},{\"name\":\"image_url1\",\"label\":\"图片1 URL\",\"uitype\":\"str\"},{\"name\":\"image_url2\",\"label\":\"图片2 URL\",\"uitype\":\"str\"},{\"name\":\"face_id\",\"label\":\"人脸ID\",\"uitype\":\"str\"}]'),('io-graph','图数据库','图CRUD输入输出','[{\"name\":\"graph\",\"label\":\"图名\",\"uitype\":\"str\",\"required\":true},{\"name\":\"query\",\"label\":\"图查询语句\",\"uitype\":\"text\"},{\"name\":\"data\",\"label\":\"图数据\",\"uitype\":\"json\"}]'),('io-ner','NER实体识别','命名实体识别','[{\"name\":\"text\",\"label\":\"待识别文本\",\"uitype\":\"text\",\"required\":true}]'),('io-reranker','Reranker重排序','BGE重排序输入输出','[{\"name\":\"query\",\"label\":\"查询文本\",\"uitype\":\"text\",\"required\":true},{\"name\":\"documents\",\"label\":\"候选文档列表\",\"uitype\":\"json\",\"required\":true}]'),('io-vdb-vector','VDB向量操作','向量数据库CRUD输入输出','[{\"name\":\"collection\",\"label\":\"集合名\",\"uitype\":\"str\",\"required\":true},{\"name\":\"data\",\"label\":\"向量数据\",\"uitype\":\"json\"},{\"name\":\"ids\",\"label\":\"向量ID列表\",\"uitype\":\"json\"},{\"name\":\"topK\",\"label\":\"返回数量\",\"uitype\":\"int\"}]');
/*!40103 SET TIME_ZONE=@OLD_TIME_ZONE */;

/*!40101 SET SQL_MODE=@OLD_SQL_MODE */;
/*!40014 SET FOREIGN_KEY_CHECKS=@OLD_FOREIGN_KEY_CHECKS */;
/*!40111 SET SQL_NOTES=@OLD_SQL_NOTES */;

