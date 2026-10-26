interface ISiteMetadataResult {
  siteTitle: string;
  siteUrl: string;
  description: string;
  logo: string;
  navLinks: {
    name: string;
    url: string;
  }[];
}

const getBasePath = () => {
  const baseUrl = import.meta.env.BASE_URL;
  return baseUrl === '/' ? '' : baseUrl;
};

const data: ISiteMetadataResult = {
  siteTitle: 'z10232 的跑步主页',
  siteUrl: 'https://z10232.github.io/running_page/',
  logo: 'https://github.com/z10232.png',
  description: 'z10232 的 Keep 跑步记录',
  navLinks: [
    {
      name: '运动汇总',
      url: `${getBasePath()}/summary`,
    },
    {
      name: 'GitHub',
      url: 'https://github.com/z10232',
    },
    {
      name: '项目说明',
      url: 'https://github.com/z10232/running_page/blob/master/README-CN.md',
    },
  ],
};

export default data;
