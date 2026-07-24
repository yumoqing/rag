// File drop upload handler - prevents browser default, uploads to API
(function(){
var kb_id = '{{params_kw.kb_id}}';
document.addEventListener('dragover', function(e){ e.preventDefault(); e.stopPropagation(); });
document.addEventListener('drop', function(e){
    e.preventDefault(); e.stopPropagation();
    var files = e.dataTransfer.files;
    if (!files.length) return;
    var st = document.getElementById('status_text');
    if (st) st.innerHTML = '上传中...';
    for (var i=0; i<files.length; i++){
        (function(f){
            var xhr = new XMLHttpRequest();
            xhr.open('POST', '/api/doc/upload?kb_id='+kb_id+'&file_name='+encodeURIComponent(f.name));
            xhr.onload = function(){
                if (xhr.status === 200){
                    if (st) st.innerHTML = '上传成功: '+f.name;
                } else {
                    if (st) st.innerHTML = '上传失败: HTTP '+xhr.status;
                }
            };
            xhr.send(f);
        })(files[i]);
    }
});
})();
