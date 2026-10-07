# Что починено в этом форке

Рабочий форк демо-приложения **Sock Shop**
(upstream: https://github.com/microservices-demo/microservices-demo),
подготовленный для запуска на локальном кластере **k3d** (k3s) на Windows/WSL2.

Ниже — отличия от upstream и их причина.

---

## 1. Регистрация пользователя падала с `Internal Server Error (500)`

### Симптом
`POST /register` через веб-интерфейс возвращал **500**, хотя сервис `user`
пользователя создавал.

### Настоящая причина (НЕ в `user`/`user-db`)
Сервисы `user` и `user-db` были полностью здоровы:

- `GET /health` у `user` отвечал `user-db: OK`;
- прямой `POST /register` в `svc/user` возвращал `200` + id покупателя.

Ошибку порождал **front-end** на этапе слияния корзины сразу после регистрации.
В кластере крутился старый образ `weaveworksdemos/front-end:0.3.1`, в котором
`api/endpoints.js` указывал на несуществующий хост `cart` (единственное число):

```js
cartsUrl: "http://cart/carts"   // BUG: сервис называется "carts"
```

а Service в кластере называется `carts` (множественное число). В логах front-end:

```
Posting Customer: {...}
{ id: '6ac649390cc5ef00019d4254' }          <- user создал покупателя OK
Merging carts for customer id: 6ac649390cc5ef00019d4254 ...
Error with log in: Error: getaddrinfo EAI_AGAIN cart:80
POST /register 500
```

### Исправление
Использовать образ, соответствующий манифестам, — `weaveworksdemos/front-end:0.3.12`,
в котором `endpoints.js` содержит корректный адрес `http://carts/carts`.

```diff
- image: weaveworksdemos/front-end:0.3.1
+ image: weaveworksdemos/front-end:0.3.12
```

Применено в `deploy/kubernetes/manifests/09-front-end-dep.yaml` и
`deploy/kubernetes/complete-demo.yaml`.

После раскатки `POST /register` возвращает `200`, в логах — `Carts merged.`.

> Почему баг вообще возникал: в кластер ранее был задеплоен образ `0.3.1`
> **вручную**, а манифесты из этого репозитория не применялись. При развёртывании
> строго через манифесты проблема не возникает — здесь фикс зафиксирован явно,
> чтобы версия не «уехала» снова.

---

## 2. Добавлен Ingress для front-end

В upstream Ingress есть только внутри helm-чарта. Для k3d добавлен отдельный
манифест `deploy/kubernetes/manifests/29-front-end-ingress.yaml`, чтобы приложение
было доступно с хоста по `http://localhost:<port>` без `kubectl port-forward` и
без NodePort.

---

## 3. Устаревшие node-селекторы обновлены

Во всех манифестах устаревший (deprecated) лейбл ноды `beta.kubernetes.io/os`
заменён на стабильный `kubernetes.io/os`, чтобы манифесты разворачивались и на
новых кластерах (k3s, GKE, EKS), где бета-лейбл может отсутствовать.
