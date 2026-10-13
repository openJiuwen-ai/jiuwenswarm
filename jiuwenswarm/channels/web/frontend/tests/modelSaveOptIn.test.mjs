import assert from 'node:assert/strict';
import test from 'node:test';
import fs from 'node:fs';

// Execute the component's actual save handler with local stand-ins; no network.
const source=fs.readFileSync(new URL('../src/features/settings/modules/models/ModelDialog.tsx',import.meta.url),'utf8');
const body=source.match(/const validateAndSave = async \(\) => \{([\s\S]*?)\n  \};/)[1];
for(const enabled of [false,true]){
 test(`model save only tests connection when explicitly enabled: ${enabled}`,async()=>{
  const calls=[];
  const values={errors:{},form:{validate(){throw Error('unexpected validation');}},validationRequestId:{current:0},
   buildEntry:()=>({model_name:'local-test'}),testOnSave:enabled,
   persist:async()=>calls.push('save'),request:async()=>calls.push('model-call'),
   buildModelValidationPayload:x=>x,setTesting(){},setSaveError(){},setValidationFailure(){},t:x=>x};
  const handler=new Function(...Object.keys(values),`return async()=>{${body}}`)(...Object.values(values));
  await handler();assert.deepEqual(calls,enabled?['model-call','save']:['save']);
 });
}
test('new model dialog defaults to no connection test',()=>{
 assert.match(source,/\[testOnSave, setTestOnSave\] = useState\(false\)/);
});
