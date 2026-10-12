from pathlib import Path
import zipfile,time
stamp=str(int(time.time()))
root=Path('artifacts/skillhubui52');root.mkdir(parents=True,exist_ok=True)
for suffix in ['click','drop','invalid']:
 name='qa-hubui-'+suffix+'-'+stamp
 with zipfile.ZipFile(root/(name+'.zip'),'w') as z:
  if suffix=='invalid':z.writestr(name+'/README.md','invalid skill package')
  else:
   z.writestr(name+'/SKILL.md','---\nname: '+name+'\ndescription: 隔离上传验收\n---\n\n# 测试技能\n仅整理示例输入。')
   z.writestr(name+'/references/nested/guide.md','# nested reference')
Path('._upload-fixtures.json').write_text(__import__('json').dumps({'stamp':stamp}))
