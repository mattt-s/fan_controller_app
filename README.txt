
let regex = /[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}:[0-9]+/g;
let matches = document.body.innerText.match(regex);
console.log(matches); // 在控制台输出匹配到的IP和端口