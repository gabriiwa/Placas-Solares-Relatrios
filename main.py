1s
Run python main.py listar-usinas
3 usina(s):

  plantId=1460  nome=Posto Makiolka (Guest)  kWp=75.0  status=01
  plantId=1408  nome=Posto Tijucas (Guest)  kWp=100.0  status=01
  plantId=1348  nome=Posto Bairro Novo (Guest)  kWp=75.0  status=01

JSON cru da primeira usina (útil pra conferir nomes de campo):
{
  "plantId": 1460,
  "deptId": 727,
  "plantName": "Posto Makiolka (Guest)",
  "owner": "4198811997",
  "timeZone": "-03:00",
  "dstTime": null,
  "address": "Santa Cândida",
  "currentPower": 6.93,
  "todayYield": 223.31,
  "monthlyYield": 2400.32,
  "totalYield": 123543.14,
  "capacity": 75.0,
  "fullLoadHour": 2.98,
  "status": "01",
  "dt": "2026-08-21 16:51:38",
  "createTime": "2024-04-26T23:03:26.000+08:00",
  "isVisitor": true,
  "type": "01",
  "gatewayType": null,
  "location": "-49.2481625000,-25.3606324000",
  "addressProvince": null,
  "addressCity": null,
  "addressDistrict": null,
  "meterFlag": 1,
  "meter2Flag": null,
  "photoUrl": "",
  "tariff": {
    "planId": 1619,
    "plantId": 1460,
    "priceDirection": "01",
    "priceType": "1",
    "fixPrice": 0.87,
    "priceUom": "19",
    "timeUsePrice": "{\"price\":[{\"startDate\":\"01-01\",\"endDate\":\"12-31\",\"timeRanges\":[{\"startTime\":\"00:00\",\"endTime\":\"24:00\",\"price\":\"0.87\"}]}],\"buyPrice\":null}",
    "updateHistoryEarn": null,
    "updateHistoryEarnDate": null
  },
  "priceDifference": null,
  "priceType": "1",
  "generationRate": 9.24
}

PLANT_IDS=1460,1408,1348
